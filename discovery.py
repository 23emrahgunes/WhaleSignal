"""Market discovery — hangi combo'lar Polymarket'te AKTIF + nasil resolve oluyor.

Iki yol:
  1. ASIL: Gamma **active-event discovery** — resmi event listeleme ile aktif
     `<asset> up/down <horizon>` marketlerini bul (insan-okunur/unix slug'a bagimli
     DEGIL). Ozellikle **1h** icin tek guvenilir yol.
  2. FAST PATH: **5m/15m** icin `<asset>-updown-<tf>-<windowunix>` slug lookup
     (`/events/slug/...`) — pencere zamani deterministik oldugundan hizli cozum.

Her kesfedilen markette **`resolution_source` + `resolution_type` ZORUNLU** cekilir
(Gamma resolutionSource/description/rules). Cozulemezse market UNKNOWN isaretlenir
(recorder guvenilir label uretemez -> egitim-disi).

Kapanmis marketler icin **resmi resolved sonuc** (outcomePrices) yoklanir; bu,
recorder'in FINAL_RESULT etiketidir (yerel fiyat kiyasi DEGIL).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from typing import Awaitable, Callable, Optional

import aiohttp

from config import Settings
from models import (
    Asset,
    AssetHorizon,
    Decision,
    Horizon,
    MarketRef,
    ResolutionType,
)

log = logging.getLogger("direction_engine.discovery")

# slug: btc-updown-5m-1699999999  (asset-updown-tf-windowunix)
_SLUG_RE = re.compile(r"(btc|eth|sol|xrp)-updown-(5m|15m|1h|60m|hourly)-(\d{6,})")
_HORIZON_ALIAS = {"5m": "5m", "15m": "15m", "1h": "1h", "60m": "1h", "hourly": "1h"}


# ---------------------------------------------------------------------------
# Saf yardimcilar (ag baglantisi olmadan test edilebilir)
# ---------------------------------------------------------------------------


def window_start(horizon: Horizon, now: Optional[float] = None) -> int:
    """Horizon penceresinin baslangic unix'i (now'i periyoda yuvarlar)."""
    now = time.time() if now is None else now
    period = horizon.seconds
    return int(now) - (int(now) % period)


def build_slug(asset: Asset, horizon: Horizon, window_unix: int) -> str:
    """Fast-path slug: `<asset>-updown-<tf>-<windowunix>`."""
    return f"{asset.value.lower()}-updown-{horizon.value}-{window_unix}"


def match_combo(text: str) -> Optional[AssetHorizon]:
    """slug/title icinden AssetHorizon cikar. Eslesme yoksa None."""
    if not text:
        return None
    m = _SLUG_RE.search(text.lower())
    if not m:
        return None
    asset_raw, tf_raw = m.group(1), m.group(2)
    tf = _HORIZON_ALIAS.get(tf_raw)
    if tf is None:
        return None
    try:
        return AssetHorizon(Asset(asset_raw.upper()), Horizon(tf))
    except ValueError:
        return None


def _more_current(new: "MarketRef", cur: "MarketRef", now: float) -> bool:
    """new, cur'a gore O ANA daha uygun (aktif/en yakin kapanan) pencere mi?

    Kisa-vade rolling marketlerde 'en gec biten' YANLIS secim; o anda ACIK olan
    (start<=now<end) ve en yakin kapanan pencere dogru olandir.
    """
    new_open = new.start_ts <= now
    cur_open = cur.start_ts <= now
    if new_open != cur_open:
        return new_open  # o an acik olani tercih et
    return new.end_ts < cur.end_ts  # ayni sinif -> en yakin kapanan


def classify_resolution(source_text: str, horizon: Horizon) -> ResolutionType:
    """Resolution kaynak metnini tipe cevir.

    Metadata metni varsa ondan siniflandirir (chainlink/binance/uma). Metin bos
    veya taninmiyorsa **UNKNOWN** doner — varsayim yapmaz (etiket guvenilirligi
    icin kritik). Not: horizon-bazli referans ADAPTORU ayri sey; bu, marketin
    GERCEK resolve kaynagidir.
    """
    t = (source_text or "").lower()
    if not t.strip():
        return ResolutionType.UNKNOWN
    if "chainlink" in t:
        return ResolutionType.CHAINLINK
    if "binance" in t:
        return ResolutionType.BINANCE_CANDLE
    if "uma" in t or "optimistic oracle" in t or "optimistic-oracle" in t:
        return ResolutionType.UMA
    return ResolutionType.UNKNOWN


def extract_resolution_source(market: dict, event: Optional[dict] = None) -> str:
    """Gamma market/event'ten resolution kaynak metnini topla (bos-guvenli)."""
    parts: list[str] = []
    for key in ("resolutionSource", "resolution_source"):
        v = market.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    # rules/description bazen 'resolves according to Chainlink...' icerir
    for key in ("description", "rules"):
        v = market.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    if event:
        for key in ("resolutionSource", "description"):
            v = event.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
    return " | ".join(parts)


def _iso_to_ts(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _load_json_field(v: object) -> object:
    """Gamma bazi alanlari JSON-string olarak dondurur (clobTokenIds vb.)."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return None
    return v


def _up_down_indices(outcomes: list) -> tuple[int, int]:
    """outcomes listesinden UP/DOWN indeksleri (varsayilan 0=up,1=down)."""
    up_idx, down_idx = 0, 1
    for i, name in enumerate(str(o).strip().lower() for o in outcomes):
        if name in ("up", "yes"):
            up_idx = i
        elif name in ("down", "no"):
            down_idx = i
    return up_idx, down_idx


def parse_event_markets(
    event: dict, combo: Optional[AssetHorizon] = None
) -> Optional[MarketRef]:
    """Gamma event JSON'undan up/down MarketRef uret.

    combo verilirse dogrulanir; verilmezse event slug'indan cikarilir. Aktif
    market yoksa None. resolution_source/type ZORUNLU cekilir.
    """
    if not isinstance(event, dict):
        return None
    slug = str(event.get("slug", ""))
    detected = match_combo(slug) or match_combo(str(event.get("title", "")))
    if combo is None:
        combo = detected
    if combo is None:
        return None
    if detected is not None and detected != combo:
        return None

    markets = event.get("markets") or []
    gm = next(
        (m for m in markets if isinstance(m, dict) and not m.get("closed", False)),
        None,
    )
    if gm is None:
        return None

    token_ids = _load_json_field(gm.get("clobTokenIds"))
    outcomes = _load_json_field(gm.get("outcomes"))
    if not isinstance(token_ids, list) or not isinstance(outcomes, list):
        return None
    if len(token_ids) < 2 or len(outcomes) < 2:
        return None
    up_idx, down_idx = _up_down_indices(outcomes)

    end_ts = _iso_to_ts(gm.get("endDate")) or _iso_to_ts(event.get("endDate"))
    if end_ts <= 0:
        return None
    start_ts = (
        _iso_to_ts(gm.get("startDate"))
        or _iso_to_ts(event.get("startDate"))
        or (end_ts - combo.horizon.seconds)
    )

    source = extract_resolution_source(gm, event)
    rtype = classify_resolution(source, combo.horizon)

    return MarketRef(
        combo=combo,
        condition_id=str(gm.get("conditionId", "")),
        slug=slug or str(gm.get("slug", "")),
        question=str(gm.get("question") or event.get("title", "")),
        up_token_id=str(token_ids[up_idx]),
        down_token_id=str(token_ids[down_idx]),
        start_ts=start_ts,
        end_ts=end_ts,
        resolution_source=source,
        resolution_type=rtype,
    )


def parse_resolved_outcome(market: dict) -> Optional[Decision]:
    """Kapanmis market JSON'undan **resmi** resolved sonucu (UP/DOWN) cikar.

    Gamma `outcomePrices` cozum sonrasi ["1","0"] / ["0","1"] olur. Bu, on-chain
    resolution'in yansimasidir (yerel fiyat kiyasi DEGIL). Belirsizse None.
    """
    if not isinstance(market, dict):
        return None
    if not market.get("closed", False):
        return None
    prices = _load_json_field(market.get("outcomePrices"))
    outcomes = _load_json_field(market.get("outcomes"))
    if not isinstance(prices, list) or len(prices) < 2:
        return None
    try:
        fvals = [float(p) for p in prices]
    except (ValueError, TypeError):
        return None
    if not isinstance(outcomes, list) or len(outcomes) < 2:
        outcomes = ["Up", "Down"]
    up_idx, down_idx = _up_down_indices(outcomes)
    # resolve olmus market: bir taraf ~1, diger ~0. Belirsiz (0.5/0.5) -> None.
    if abs(fvals[up_idx] - fvals[down_idx]) < 0.5:
        return None
    return Decision.UP if fvals[up_idx] > fvals[down_idx] else Decision.DOWN


# ---------------------------------------------------------------------------
# Discovery poller (ag)
# ---------------------------------------------------------------------------


class MarketDiscovery:
    """Aktif marketleri kesfeder + kapanislari resmi sonucla eslestirir.

    `active` = combo.key -> mevcut aktif MarketRef. `on_resolved` callback'i her
    resmi resolve olan (izlenen) market icin bir kez cagrilir (recorder etiketler).
    """

    def __init__(
        self,
        settings: Settings,
        session: aiohttp.ClientSession,
        combos: list[AssetHorizon],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._session = session
        self.combos = combos
        self._sleep = sleep
        self.active: dict[str, MarketRef] = {}
        self._tracked: dict[str, MarketRef] = {}  # condition_id -> ref (resolve bekleyen)
        self._resolved_seen: set[str] = set()
        self.resolved_log: deque[MarketRef] = deque(maxlen=200)
        self._on_resolved: list[Callable[[MarketRef], None]] = []
        self._lock = asyncio.Lock()
        self.last_discovery_ts: float = 0.0

    def on_resolved(self, cb: Callable[[MarketRef], None]) -> None:
        self._on_resolved.append(cb)

    def snapshot_active(self) -> dict[str, MarketRef]:
        return dict(self.active)

    async def _fetch_json(self, url: str, params: Optional[dict] = None) -> object:
        async with self._session.get(url, params=params, timeout=12) as resp:
            if resp.status != 200:
                return None
            return await resp.json()

    async def _fast_path_slug(self, combo: AssetHorizon) -> Optional[MarketRef]:
        """5m/15m: deterministik pencere slug'i ile hizli cozum."""
        win = window_start(combo.horizon)
        slug = build_slug(combo.asset, combo.horizon, win)
        url = f"{self.settings.gamma_host}/events/slug/{slug}"
        ev = await self._fetch_json(url)
        if not isinstance(ev, dict) or ev.get("closed"):
            return None
        return parse_event_markets(ev, combo)

    async def _active_event_discovery(self) -> dict[str, MarketRef]:
        """ASIL yol: Gamma active-event listeleme -> combo eslesmesi (1h dahil)."""
        found: dict[str, MarketRef] = {}
        url = f"{self.settings.gamma_host}/events"
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(self.settings.gamma_event_limit),
            "order": "startDate",
            "ascending": "false",
        }
        data = await self._fetch_json(url, params)
        if not isinstance(data, list):
            return found
        wanted = {c.key for c in self.combos}
        now = time.time()
        for ev in data:
            if not isinstance(ev, dict):
                continue
            combo = match_combo(str(ev.get("slug", ""))) or match_combo(
                str(ev.get("title", ""))
            )
            if combo is None or combo.key not in wanted:
                continue
            ref = parse_event_markets(ev, combo)
            if ref is None or ref.end_ts <= now:
                continue  # bitmis pencereyi alma
            # ayni combo icin O ANKI (aktif/en yakin) pencereyi sec — en gec biteni DEGIL
            cur = found.get(combo.key)
            if cur is None or _more_current(ref, cur, now):
                found[combo.key] = ref
        return found

    async def discover_once(self) -> None:
        found: dict[str, MarketRef] = {}
        # 1) 5m/15m: fast path ONCE — deterministik O ANKI pencere (guvenilir)
        for combo in self.combos:
            if combo.horizon in (Horizon.H5M, Horizon.H15M):
                try:
                    ref = await self._fast_path_slug(combo)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    ref = None
                if ref is not None and ref.remaining_sec() > 0:
                    found[combo.key] = ref
        # 2) active-event discovery: 1h + fast path'in bulamadigi combo'lar
        ae = await self._active_event_discovery()
        for key, ref in ae.items():
            if key not in found:
                found[key] = ref
        async with self._lock:
            for key, ref in found.items():
                prev = self.active.get(key)
                self.active[key] = ref
                if ref.condition_id:
                    self._tracked[ref.condition_id] = ref
                if not ref.has_resolution_meta:
                    log.warning(
                        "%s: resolution metadata YOK (source bos/taninmiyor) -> egitim-disi",
                        key,
                    )
                if prev is None or prev.condition_id != ref.condition_id:
                    log.info(
                        "AKTIF market %s slug=%s resolve=%s kalan=%.0fs",
                        key,
                        ref.slug,
                        ref.resolution_type.value,
                        ref.remaining_sec(),
                    )
            self.last_discovery_ts = time.time()

    async def _poll_resolutions(self) -> None:
        """Izlenen kapanmis marketlerin RESMI sonucunu yokla; on_resolved tetikle."""
        pending = [
            ref
            for cid, ref in list(self._tracked.items())
            if cid and cid not in self._resolved_seen and ref.remaining_sec() < 30
        ]
        for ref in pending:
            url = f"{self.settings.gamma_host}/markets"
            data = await self._fetch_json(url, {"condition_ids": ref.condition_id})
            market = None
            if isinstance(data, list) and data:
                market = data[0]
            elif isinstance(data, dict):
                market = data
            if not isinstance(market, dict):
                continue
            outcome = parse_resolved_outcome(market)
            if outcome is None:
                continue
            ref.resolved = True
            ref.resolved_outcome = outcome
            self._resolved_seen.add(ref.condition_id)
            self.resolved_log.appendleft(ref)
            log.info("RESOLVED %s -> %s (resmi)", ref.combo.key, outcome.value)
            for cb in self._on_resolved:
                try:
                    cb(ref)
                except Exception as exc:  # noqa: BLE001
                    log.warning("on_resolved callback hatasi: %s", exc)

    async def run(self, stop: asyncio.Event) -> None:
        last_res = 0.0
        while not stop.is_set():
            try:
                await self.discover_once()
                now = time.time()
                if now - last_res >= self.settings.resolution_poll_sec:
                    await self._poll_resolutions()
                    last_res = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("discovery dongusu hatasi: %s", exc)
            with _suppress_timeout():
                await asyncio.wait_for(stop.wait(), timeout=self.settings.gamma_poll_sec)
        log.info("discovery durduruldu")


class _suppress_timeout:
    """asyncio.TimeoutError'i yutan minik context manager (import sadeligi)."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is asyncio.TimeoutError
