"""CLOB emir yurutucu (SIM / DRY / LIVE).

`pm-edge-clean/executor_bridge.py` deseninin birebir aynasi: resmi
`py_clob_client_v2` kutuphanesi V2 + signatureType (POLY_1271/type3 dahil) ile
imzalar. Imzalama Python'da resmi kutuphaneye yaptirilir.

- SIM : gercek istemci YOK; emirler sadece simulatore gider (bu sinif no-op ack).
- DRY : `create_order` -> imzalar ama POST etmez (creds/imza dogrulama).
- LIVE: `create_and_post_order` -> gercek emir CLOB'a gider. Yalniz EXEC_MODE=LIVE.

`py_clob_client_v2` yalniz DRY/LIVE'da, tembel (lazy) import edilir; boylece SIM
ve testler bagimlilik olmadan calisir.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from config import Settings
from models import ExecMode, Side

log = logging.getLogger("dual_arbitraj.exec")


class ClobExecutor:
    """0.40 Up/Down limit emirleri icin ince yurutme katmani."""

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.mode = cfg.exec_mode
        self._client: Any = None  # ClobClient (DRY/LIVE) veya None (SIM)
        self._sim_seq = 0

    # ---- Istemci kurulumu (lazy) --------------------------------------------

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self.mode == ExecMode.SIM:
            return None
        ok, why = self.cfg.live_ready()
        if not ok:
            raise RuntimeError(f"LIVE icin eksik konfig: {why}")
        # Resmi kutuphane yalniz burada import edilir.
        from py_clob_client_v2 import ApiCreds, ClobClient  # type: ignore

        kwargs: dict[str, Any] = {
            "host": self.cfg.clob_host,
            "chain_id": self.cfg.chain_id,
            "key": self.cfg.private_key,
        }
        if self.cfg.clob_api_key:
            kwargs["creds"] = ApiCreds(
                api_key=self.cfg.clob_api_key,
                api_secret=self.cfg.clob_api_secret,
                api_passphrase=self.cfg.clob_api_passphrase,
            )
        if self.cfg.funder_address:
            kwargs["funder"] = self.cfg.funder_address
        if self.cfg.signature_type is not None:
            kwargs["signature_type"] = int(self.cfg.signature_type)
        self._client = ClobClient(**kwargs)
        log.info(
            "CLOB CLIENT KURULDU mode=%s host=%s funder=%s sigType=%s",
            self.mode.value,
            self.cfg.clob_host,
            self.cfg.funder_address,
            self.cfg.signature_type,
        )
        return self._client

    def _order_options(self) -> Any:
        from py_clob_client_v2.clob_types import PartialCreateOrderOptions  # type: ignore

        if self.cfg.neg_risk:
            return PartialCreateOrderOptions(
                tick_size=self.cfg.order_tick_size, neg_risk=True
            )
        return PartialCreateOrderOptions(tick_size=self.cfg.order_tick_size)

    # ---- Emir islemleri -----------------------------------------------------

    def place(
        self,
        token_id: str,
        side: Side,
        size: float,
        price: float,
        order_type: str = "GTC",
    ) -> dict[str, Any]:
        """Bir bacak icin limit emir. Mode'a gore SIM/DRY/LIVE davranisi."""
        if self.mode == ExecMode.SIM:
            self._sim_seq += 1
            oid = f"sim-{self._sim_seq}"
            log.debug("SIM emir %s %s %.2f@%.3f id=%s", side.value, token_id, size, price, oid)
            return {"ok": True, "mode": "SIM", "orderId": oid, "posted": False}

        client = self._ensure_client()
        from py_clob_client_v2 import OrderArgs  # type: ignore

        args = OrderArgs(
            token_id=token_id, price=float(price), size=float(size), side=side.value
        )
        opts = self._order_options()

        if self.mode == ExecMode.DRY:
            signed = client.create_order(args, opts)  # imzalar, POST YOK
            return {"ok": True, "mode": "DRY", "posted": False, "signed": _safe(signed)}

        # LIVE
        resp = client.create_and_post_order(args, opts, order_type)
        oid = _extract_order_id(resp)
        return {"ok": True, "mode": "LIVE", "posted": True, "orderId": oid, "raw": _safe(resp)}

    def cancel(self, order_id: Optional[str]) -> dict[str, Any]:
        if not order_id:
            return {"ok": True, "noop": True}
        if self.mode == ExecMode.SIM:
            log.debug("SIM iptal id=%s", order_id)
            return {"ok": True, "mode": "SIM", "canceled": order_id}
        client = self._ensure_client()
        resp = client.cancel(order_id)
        return {"ok": True, "mode": self.mode.value, "canceled": order_id, "raw": _safe(resp)}


# ---- yardimcilar ------------------------------------------------------------


def _safe(obj: Any) -> Any:
    """Kutuphane nesnesini JSON-guvenli hale getir (dict/list/skaler)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe(v) for v in obj]
    for attr in ("model_dump", "dict", "__dict__"):
        val = getattr(obj, attr, None)
        if callable(val):
            try:
                return _safe(val())
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(val, dict):
            return _safe(val)
    return str(obj)


def _extract_order_id(resp: Any) -> Optional[str]:
    data = _safe(resp)
    if isinstance(data, dict):
        for key in ("orderID", "orderId", "id"):
            if data.get(key):
                return str(data[key])
    return None
