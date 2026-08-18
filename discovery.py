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
    DiscoveryStatus,
    Horizon,
    LabelStatus,
    MarketRef,
    ResolutionType,
    TimeStatus,
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


def parse_slug_unix(slug: str) -> Optional[int]:
    """slug'daki pencere unix'i (canonical market_start). Yoksa None."""
    if not slug:
        return None
    m = _SLUG_RE.search(slug.lower())
    if not m:
        return None
    try:
        return int(m.group(3))
    except (ValueError, TypeError):
        return None


def canonical_time(
    combo: AssetHorizon, slug: str, meta_start: float, meta_end: float
) -> tuple[Optional[float], Optional[float], TimeStatus]:
    """Canonical market_start/end + time_status uret.

    5m/15m: slug-unix OTORİTER (market_start=unix, market_end=unix+horizon). Generic
    endDate yalnız cross-check (buyuk sapma -> WARNING, UNSAFE DEGIL). slug-unix yoksa
    5m/15m icin UNSAFE_TIME_METADATA. 1h: slug-unix varsa o, yoksa metadata; canonical
    sure horizon'a uymuyorsa UNSAFE.
    """
    horizon_sec = combo.horizon.seconds
    slug_unix = parse_slug_unix(slug)
    if slug_unix:
        start = float(slug_unix)
        end = float(slug_unix + horizon_sec)
        if meta_end > 0 and abs(meta_end - end) > 120:
            log.warning(
                "%s canonical/endDate uyusmuyor (canon_end=%.0f meta_end=%.0f) -> canonical'a guveniliyor",
                slug, end, meta_end,
            )
        return start, end, TimeStatus.OK
    # slug-unix yok
    if combo.horizon in (Horizon.H5M, Horizon.H15M):
        # 5m/15m'de slug-unix sart; yoksa metadata guvenilmez
        return (meta_start or None), (meta_end or None), TimeStatus.UNSAFE_TIME_METADATA
    # 1h: metadata start/end
    if meta_end <= 0:
        return None, None, TimeStatus.UNSAFE_TIME_METADATA
    start = meta_start if meta_start > 0 else (meta_end - horizon_sec)
    end = meta_end
    # canonical sure horizon'a makul uymali
    if abs((end - start) - horizon_sec) > max(120, 0.25 * horizon_sec):
        return start, end, TimeStatus.UNSAFE_TIME_METADATA
    return start, end, TimeStatus.OK


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
    new_open = (new.market_start_ts or new.start_ts) <= now
    cur_open = (cur.market_start_ts or cur.start_ts) <= now
    if new_open != cur_open:
        return new_open  # o an acik olani tercih et
    # ayni sinif -> canonical end en yakin kapanan
    return (new.market_end_ts or new.end_ts) < (cur.market_end_ts or cur.end_ts)


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
        # TWAP (time-weighted) ayri tip -> pencere+gozlem ani saklanir
        if "twap" in t or "time-weighted" in t or "time weighted" in t:
            return ResolutionType.CHAINLINK_TWAP
        return ResolutionType.CHAINLINK
    if "binance" in t:
        return ResolutionType.BINANCE_CANDLE
    if "uma" in t or "optimistic oracle" in t or "optimistic-oracle" in t:
        return ResolutionType.UMA
    return ResolutionType.UNKNOWN


def parse_official_result(
    market: dict, up_token_id: str = "", down_token_id: str = ""
) -> tuple[Optional[Decision], str]:
    """**Explicit official** resolution -> (Decision, kaynak_notu).

    Oncelik: (1) explicit winner outcome adi, (2) winning asset/token id -> up/down
    eslesme, (3) explicit resolution STATUS (umaResolutionStatus/resolved) onaylıysa
    outcomePrices'tan turet. Hicbiri yoksa (None, "none") — labeled sayma.
    Gamma `outcomePrices` TEK BASINA official DEGIL; yalniz status onayiyla gecerli.
    """
    if not isinstance(market, dict):
        return None, "none"
    # 1) explicit winner outcome adi
    for k in ("winning_outcome", "winningOutcome", "winner"):
        v = market.get(k)
        if isinstance(v, str) and v.strip():
            name = v.strip().lower()
            if name in ("up", "yes"):
                return Decision.UP, "winning_outcome"
            if name in ("down", "no"):
                return Decision.DOWN, "winning_outcome"
    # 2) winning asset/token id -> up/down token eslesme
    for k in ("winning_asset_id", "winningTokenId", "winning_token_id", "resolvedTokenId"):
        v = market.get(k)
        if v is not None and str(v).strip():
            s = str(v).strip()
            if up_token_id and s == up_token_id:
                return Decision.UP, "winning_asset_id"
            if down_token_id and s == down_token_id:
                return Decision.DOWN, "winning_asset_id"
    # 3) explicit resolution status onayi + outcomePrices
    status = str(
        market.get("umaResolutionStatus") or market.get("resolutionStatus") or ""
    ).lower()
    resolved_flag = bool(market.get("resolved")) or "resolved" in status
    if resolved_flag:
        oc = parse_resolved_outcome(market)  # outcomePrices
        if oc is not None:
            return oc, "resolved_status+outcomePrices"
    return None, "none"


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

    # HAM metadata zaman (cross-check icin)
    meta_end = _iso_to_ts(gm.get("endDate")) or _iso_to_ts(event.get("endDate"))
    meta_start = (
        _iso_to_ts(gm.get("startDate"))
        or _iso_to_ts(event.get("startDate"))
        or (meta_end - combo.horizon.seconds if meta_end > 0 else 0.0)
    )
    the_slug = slug or str(gm.get("slug", ""))
    # CANONICAL zaman (5m/15m slug-unix OTORİTER)
    market_start, market_end, time_status = canonical_time(
        combo, the_slug, meta_start, meta_end
    )
    if market_end is None or market_start is None:
        # canonical zaman hic kurulamadi -> market yerlestirilemez
        return None

    source = extract_resolution_source(gm, event)
    rtype = classify_resolution(source, combo.horizon)

    return MarketRef(
        combo=combo,
        condition_id=str(gm.get("conditionId", "")),
        slug=the_slug,
        question=str(gm.get("question") or event.get("title", "")),
        up_token_id=str(token_ids[up_idx]),
        down_token_id=str(token_ids[down_idx]),
        start_ts=meta_start,
        end_ts=meta_end,
        market_start_ts=market_start,
        market_end_ts=market_end,
        time_status=time_status,
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
        # combo.key -> DiscoveryStatus (generic 'YOK' yerine 4 durum)
        self.status: dict[str, DiscoveryStatus] = {
            c.key: DiscoveryStatus.NOT_FOUND for c in combos
        }
        self.discovery_errors: int = 0

    def on_resolved(self, cb: Callable[[MarketRef], None]) -> None:
        self._on_resolved.append(cb)

    def snapshot_active(self) -> dict[str, MarketRef]:
        return dict(self.active)

    def snapshot_status(self) -> dict[str, str]:
        return {k: v.value for k, v in self.status.items()}

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
            if ref is None or ref.remaining_sec(now) <= 0:
                continue  # bitmis pencereyi alma (canonical)
            # ayni combo icin O ANKI (aktif/en yakin) pencereyi sec — en gec biteni DEGIL
            cur = found.get(combo.key)
            if cur is None or _more_current(ref, cur, now):
                found[combo.key] = ref
        return found

    async def discover_once(self) -> None:
        found: dict[str, MarketRef] = {}
        errored: set[str] = set()
        now = time.time()
        # 1) 5m/15m: fast path ONCE — deterministik O ANKI pencere (guvenilir)
        for combo in self.combos:
            if combo.horizon in (Horizon.H5M, Horizon.H15M):
                try:
                    ref = await self._fast_path_slug(combo)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    self.discovery_errors += 1
                    errored.add(combo.key)
                    ref = None
                if ref is not None and ref.remaining_sec(now) > 0:
                    found[combo.key] = ref
        # 2) active-event discovery: 1h + fast path'in bulamadigi combo'lar
        try:
            ae = await self._active_event_discovery()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.discovery_errors += 1
            ae = {}
            errored.update(c.key for c in self.combos if c.key not in found)
        for key, ref in ae.items():
            if key not in found:
                found[key] = ref
        async with self._lock:
            for key, ref in found.items():
                prev = self.active.get(key)
                self.active[key] = ref
                self.status[key] = DiscoveryStatus.FOUND
                if ref.condition_id:
                    self._tracked[ref.condition_id] = ref
                if not ref.has_resolution_meta:
                    log.warning(
                        "%s: resolution metadata YOK (source bos/taninmiyor) -> egitim-disi",
                        key,
                    )
                if prev is None or prev.condition_id != ref.condition_id:
                    log.info(
                        "AKTIF market %s slug=%s resolve=%s canon_kalan=%.0fs time=%s",
                        key, ref.slug, ref.resolution_type.value,
                        ref.remaining_sec(now), ref.time_status.value,
                    )
            # bulunmayan combo'lar: hata mi, gercekten yok mu
            for combo in self.combos:
                if combo.key in found:
                    continue
                self.active.pop(combo.key, None)
                self.status[combo.key] = (
                    DiscoveryStatus.DISCOVERY_ERROR
                    if combo.key in errored
                    else DiscoveryStatus.NOT_FOUND
                )
            self.last_discovery_ts = time.time()

    async def _poll_resolutions(self) -> None:
        """Kapanmis marketlerin **EXPLICIT official** sonucunu yokla; on_resolved tetikle.

        official = explicit resolution metadata (winning_outcome/asset_id/status). Gamma
        outcomePrices yalniz **sanity-check** (label_status). official yoksa beklemeye devam.
        """
        now = time.time()
        pending = [
            ref
            for cid, ref in list(self._tracked.items())
            if cid and cid not in self._resolved_seen and ref.remaining_sec(now) < 30
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
            official, note = parse_official_result(
                market, ref.up_token_id, ref.down_token_id
            )
            if official is None:
                continue  # explicit official yok -> bekle (labeled sayma)
            sanity = parse_resolved_outcome(market)  # outcomePrices (sanity-check)
            if sanity is not None and sanity != official:
                ref.label_status = LabelStatus.MISMATCH
            else:
                ref.label_status = LabelStatus.MATCH
            ref.resolved = True
            ref.official_result = official
            ref.resolved_outcome = official  # geriye uyum
            self._resolved_seen.add(ref.condition_id)
            self.resolved_log.appendleft(ref)
            log.info(
                "RESOLVED %s -> %s (official via %s, label=%s)",
                ref.combo.key, official.value, note, ref.label_status.value,
            )
            for cb in self._on_resolved:
                try:
                    cb(ref)
                except Exception as exc:  # noqa: BLE001
                    log.warning("on_resolved callback hatasi: %s", exc)

    async def backfill_resolved(self, n: int, sink: Callable[[MarketRef], None]) -> int:
        """Son N resolved *supported* market'i cek, EXPLICIT official ile `sink`e ver.

        Yalniz market + resolution + label pipeline testi. **Snapshot/feature URETMEZ.**
        """
        loaded = 0
        url = f"{self.settings.gamma_host}/events"
        params = {
            "closed": "true", "limit": str(max(50, n * 10)),
            "order": "endDate", "ascending": "false",
        }
        data = await self._fetch_json(url, params)
        if not isinstance(data, list):
            return 0
        wanted = {c.key for c in self.combos}
        for ev in data:
            if loaded >= n:
                break
            if not isinstance(ev, dict):
                continue
            combo = match_combo(str(ev.get("slug", ""))) or match_combo(str(ev.get("title", "")))
            if combo is None or combo.key not in wanted:
                continue
            markets = ev.get("markets") or []
            gm = markets[0] if markets else None
            if not isinstance(gm, dict):
                continue
            ref = parse_event_markets({**ev, "markets": [gm]}, combo)
            if ref is None:
                continue
            official, note = parse_official_result(gm, ref.up_token_id, ref.down_token_id)
            if official is None:
                continue
            sanity = parse_resolved_outcome(gm)
            ref.label_status = (
                LabelStatus.MISMATCH if (sanity is not None and sanity != official)
                else LabelStatus.MATCH
            )
            ref.resolved = True
            ref.official_result = official
            ref.resolved_outcome = official
            try:
                sink(ref)
                loaded += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("backfill sink hatasi: %s", exc)
        return loaded

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
