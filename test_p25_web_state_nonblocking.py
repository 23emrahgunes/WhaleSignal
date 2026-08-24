"""Regression guard for P2.5 dashboard event-loop responsiveness."""
from pathlib import Path


def test_p25_state_snapshot_runs_off_aiohttp_event_loop():
    text = Path("p25_web_records.py").read_text(encoding="utf-8")
    assert "payload = await asyncio.to_thread(engine.snapshot)" in text
    assert "payload = engine.snapshot()" not in text
    assert "Build a constant-time liveness payload without engine.snapshot/SQLite." in text
