"""CLOB Executor Bridge — resmi py-clob-client v2'yi localhost HTTP servisiyle sarar.

pm-edge (Go) beyni karar verir, buraya HTTP ile "bas/iptal/dolum" komutu yollar;
bu servis GERCEK V2 + signatureType (POLY_1271 dahil) imzalamayi resmi kutuphaneye
yaptirir. Boylece imza dogrulugu kanitlanmis kodla garanti.

Calistir:  .venv/Scripts/python executor_bridge.py   (Linux: .venv/bin/python)
Guvenlik:  yalniz 127.0.0.1'e baglanir + X-Executor-Token basligi (EXECUTOR_TOKEN).

Env: CLOB_HOST, CHAIN_ID, PRIVATE_KEY(/PK), API_KEY/SECRET/PASSPHRASE(/CLOB_API_*),
     FUNDER_ADDRESS, SIGNATURE_TYPE, ORDER_TICK_SIZE, NEG_RISK,
     EXECUTOR_PORT(=8099), EXECUTOR_TOKEN(paylasilan sir).
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("executor_bridge")


def _load_dotenv(path: str) -> None:
    """Minimal .env yukleyici (bagimlilik yok). Var olan env'i EZMEZ."""
    if not path or not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v not in (None, ""):
            return v
    return default


def build_client():
    from py_clob_client_v2 import ClobClient, ApiCreds  # noqa: WPS433

    host = _env("CLOB_HOST", default="https://clob.polymarket.com")
    chain_id = int(_env("CHAIN_ID", default="137"))
    key = _env("PRIVATE_KEY", "PK", "CLOB_PRIVATE_KEY")
    api_key = _env("API_KEY", "CLOB_API_KEY")
    api_secret = _env("API_SECRET", "SECRET", "CLOB_API_SECRET")
    api_pass = _env("API_PASSPHRASE", "PASSPHRASE", "CLOB_API_PASSPHRASE")
    funder = _env("FUNDER_ADDRESS")
    sig_type = _env("SIGNATURE_TYPE")

    kwargs = {"host": host, "chain_id": chain_id}
    if key:
        kwargs["key"] = key
    if api_key and api_secret and api_pass:
        kwargs["creds"] = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass)
    if funder:
        kwargs["funder"] = funder
    if sig_type is not None:
        kwargs["signature_type"] = int(sig_type)

    client = ClobClient(**kwargs)
    log.warning(
        "CLOB CLIENT KURULDU host=%s chain=%s funder=%s sigType=%s creds=%s",
        host, chain_id, funder, sig_type, bool(api_key),
    )
    return client


class Executor:
    def __init__(self) -> None:
        self.client = build_client()
        self.tick_size = _env("ORDER_TICK_SIZE", default="0.01")
        self.neg_risk = _env("NEG_RISK", default="false").lower() in ("1", "true", "yes")

    def _order_args(self, token_id: str, side: str, size: float, price: float):
        from py_clob_client_v2 import OrderArgs

        return OrderArgs(token_id=token_id, price=float(price), size=float(size), side=side.upper())

    def _options(self):
        from py_clob_client_v2 import PartialCreateOrderOptions

        try:
            return PartialCreateOrderOptions(tick_size=self.tick_size, neg_risk=self.neg_risk)
        except TypeError:
            return PartialCreateOrderOptions(tick_size=self.tick_size)

    def place(self, token_id: str, side: str, size: float, price: float, order_type: str, dry: bool) -> dict:
        from py_clob_client_v2 import OrderType

        args = self._order_args(token_id, side, size, price)
        opts = self._options()
        if dry:
            # Yalniz IMZALA (POST yok) — imza/tutar dogrulamasi icin guvenli.
            signed = self.client.create_order(args, opts)
            return {"ok": True, "dry": True, "signed": _safe(signed)}
        otype = getattr(OrderType, order_type.upper(), OrderType.GTC)
        resp = self.client.create_and_post_order(args, opts, otype)
        oid = _order_id(resp)
        return {"ok": bool(oid) or _resp_ok(resp), "orderId": oid, "raw": _safe(resp)}

    def cancel(self, order_id: str) -> dict:
        try:
            from py_clob_client_v2 import OrderPayload  # type: ignore

            payload = OrderPayload(orderID=order_id)
        except Exception:
            payload = SimpleNamespace(orderID=order_id)
        resp = self.client.cancel_order(payload)
        return {"ok": True, "raw": _safe(resp)}

    def order(self, order_id: str) -> dict:
        resp = self.client.get_order(order_id)
        matched = 0.0
        if isinstance(resp, dict):
            for k in ("size_matched", "sizeMatched"):
                if resp.get(k) is not None:
                    matched = float(resp[k])
                    break
        return {"ok": True, "sizeMatched": matched, "raw": _safe(resp)}


def _order_id(resp) -> str:
    if isinstance(resp, dict):
        return str(resp.get("orderID") or resp.get("orderId") or "")
    return str(getattr(resp, "orderID", "") or getattr(resp, "orderId", "") or "")


def _resp_ok(resp) -> bool:
    if isinstance(resp, dict):
        return resp.get("success") is not False
    return True


def _safe(obj):
    try:
        json.dumps(obj)
        return obj
    except Exception:
        return str(obj)


def make_handler(executor: Executor, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # sessiz (kendi logumuz var)
            pass

        def _authed(self) -> bool:
            return not token or self.headers.get("X-Executor-Token") == token

        def _send(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True})
                return
            if not self._authed():
                self._send(401, {"ok": False, "error": "token"})
                return
            if self.path.startswith("/order"):
                oid = self.path.split("id=", 1)[-1] if "id=" in self.path else ""
                try:
                    self._send(200, executor.order(oid))
                except Exception as exc:  # noqa: BLE001
                    self._send(500, {"ok": False, "error": str(exc)})
                return
            self._send(404, {"ok": False})

        def do_POST(self):
            if not self._authed():
                self._send(401, {"ok": False, "error": "token"})
                return
            b = self._body()
            try:
                if self.path == "/place":
                    res = executor.place(
                        b["token_id"], b.get("side", "BUY"), float(b["size"]),
                        float(b["price"]), b.get("type", "GTC"), bool(b.get("dry", False)),
                    )
                    self._send(200, res)
                elif self.path == "/cancel":
                    self._send(200, executor.cancel(b["order_id"]))
                else:
                    self._send(404, {"ok": False})
            except Exception as exc:  # noqa: BLE001
                log.exception("istek hatasi")
                self._send(500, {"ok": False, "error": str(exc)})

    return Handler


def main():
    _load_dotenv(os.getenv("ENV_FILE", ".env"))
    port = int(_env("EXECUTOR_PORT", default="8099"))
    token = _env("EXECUTOR_TOKEN", default="")
    if not token:
        log.warning("EXECUTOR_TOKEN bos -> kimlik dogrulama YOK (yalniz 127.0.0.1). Guclu bir token oner.")
    executor = Executor()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(executor, token))
    log.warning("EXECUTOR BRIDGE DINLIYOR http://127.0.0.1:%s (token=%s)", port, bool(token))
    server.serve_forever()


if __name__ == "__main__":
    main()
