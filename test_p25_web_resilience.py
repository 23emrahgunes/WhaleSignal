from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from p25_paper_engine import P25Engine
from p25_web_records import _health_payload, _main_html_with_paper_link


def test_health_payload_is_constant_time_and_never_calls_snapshot():
    class Engine:
        latest = {
            "BTC:5m": {"active": True},
            "ETH:5m": {"active": False},
            "SOL:5m": {"active": True},
        }

        @staticmethod
        def snapshot():
            raise AssertionError("health must not execute engine.snapshot")

    cfg = SimpleNamespace(phase="P2.5", paper_trading_enabled=True)
    payload = _health_payload(Engine(), cfg)
    assert payload["ok"] is True
    assert payload["phase"] == "P2.5"
    assert payload["markets_active"] == 2
    assert payload["paper_trading_enabled"] is True
    assert payload["execution_enabled"] is False
    assert payload["live_orders"] == 0


def test_dashboard_polling_is_throttled_to_three_seconds():
    html = _main_html_with_paper_link()
    assert "setInterval(tick,3000);tick();" in html
    assert "setInterval(tick,1500);tick();" not in html
    assert "PAPER KAYITLARI" in html


def test_paper_analytics_are_cached_and_expire(monkeypatch):
    class Recorder:
        def __init__(self) -> None:
            self.calls = 0

        def paper_analytics(self, _limit: int) -> dict:
            self.calls += 1
            return {
                "enabled": True,
                "paper_only": True,
                "overall": {"attempts": self.calls},
                "per_asset": {},
                "per_horizon": {},
                "per_combo": {},
                "recent_markets": [],
                "open_positions": [],
            }

    engine = object.__new__(P25Engine)
    engine.recorder = Recorder()
    engine.cfg = SimpleNamespace(paper_recent_limit=50)
    monkeypatch.setenv("P25_PAPER_ANALYTICS_CACHE_SEC", "60")

    first = engine._paper_analytics_cached()
    second = engine._paper_analytics_cached()
    assert first is second
    assert engine.recorder.calls == 1

    engine._paper_analytics_cache_at -= 61.0
    third = engine._paper_analytics_cached()
    assert third["overall"]["attempts"] == 2
    assert engine.recorder.calls == 2


def test_web_routes_use_cached_state_and_summary():
    source = Path("p25_web_records.py").read_text(encoding="utf-8")
    assert "return web.json_response(await cached_state())" in source
    assert "return web.json_response(await cached_paper_summary())" in source
    assert "return web.json_response(_health_payload(engine, cfg))" in source
    assert '"P25_WEB_SUMMARY_FORECAST_ANALYTICS", False' in source
