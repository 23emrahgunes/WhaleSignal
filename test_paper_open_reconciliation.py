"""Regression tests for paper trades left OPEN across a deployment restart."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from p25_paper_config import PaperSettings
from p25_paper_reconcile import PaperTradeReconciler
from p25_reconciling_recorder import P25ReconcilingPaperRecorder


def _settings(monkeypatch, db_path: str) -> PaperSettings:
    monkeypatch.setenv("PHASE", "P2.5")
    monkeypatch.setenv("MODEL_TRAINING_ENABLED", "false")
    monkeypatch.setenv("CALIBRATION_ENABLED", "false")
    monkeypatch.setenv("FORECAST_RECORDING_ENABLED", "true")
    monkeypatch.setenv("PAPER_TRADING_ENABLED", "true")
    monkeypatch.setenv("DB_PATH", db_path)
    cfg = PaperSettings()
    cfg.enforce_phase_lock()
    return cfg


def _insert_open(
    recorder: P25ReconcilingPaperRecorder,
    *,
    condition_id: str = "0xopen",
    slug: str = "sol-updown-15m-1787185800",
    side: str = "UP",
) -> None:
    recorder.conn.execute(
        """
        INSERT INTO paper_trades (
            condition_id, market_id, combo_key, asset, horizon, slug,
            strategy_version, checkpoint_sec, attempted_at, entry_tte_sec,
            side, forecast_p_up, selected_probability, forecast_confidence,
            forecast_grade, forecast_status, forecast_agreement,
            entry_bid, entry_ask, fill_price, forecast_edge,
            stake_usdc, shares, slippage, fee_usdc, status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            condition_id,
            "market-open",
            "SOL:15m",
            "SOL",
            "15m",
            slug,
            recorder.paper_policy.strategy_version,
            240,
            1_787_185_800.0,
            240.0,
            side,
            0.73,
            0.73,
            0.31,
            "LOW",
            "VALIDATED",
            0.70,
            0.59,
            0.60,
            0.605,
            0.125,
            2.50,
            2.50 / 0.605,
            0.005,
            0.0,
            "OPEN",
        ),
    )
    recorder.conn.commit()


def _resolved_market(condition_id: str, result: str = "UP") -> dict:
    prices = '["1", "0"]' if result == "UP" else '["0", "1"]'
    return {
        "id": "market-open",
        "conditionId": condition_id,
        "slug": "sol-updown-15m-1787185800",
        "closed": True,
        "automaticallyResolved": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Up", "Down"]',
        "outcomePrices": prices,
    }


class FakeDiscovery:
    def __init__(self, event_payload, fallback=None):  # noqa: ANN001
        self.event_payload = event_payload
        self.fallback = [] if fallback is None else fallback
        self.calls: list[tuple[str, object]] = []

    async def _fetch_json(self, url: str, params=None):  # noqa: ANN001
        self.calls.append((url, params))
        if "/events/slug/" in url:
            return self.event_payload
        if url.endswith("/markets"):
            return self.fallback
        raise AssertionError(f"unexpected URL {url}")


@pytest.mark.asyncio
async def test_restart_orphan_open_trade_is_settled(tmp_path, monkeypatch):
    cfg = _settings(monkeypatch, str(tmp_path / "paper.sqlite"))
    recorder = P25ReconcilingPaperRecorder(cfg.db_path, cfg)
    try:
        _insert_open(recorder)
        discovery = FakeDiscovery(
            {"markets": [_resolved_market("0xopen", "UP")]}
        )
        reconciler = PaperTradeReconciler(cfg, discovery, recorder)

        stats = await reconciler.reconcile_once()
        row = recorder.conn.execute(
            "SELECT status, official_result, correct, realized_pnl, roi "
            "FROM paper_trades WHERE condition_id='0xopen'"
        ).fetchone()

        assert row["status"] == "SETTLED"
        assert row["official_result"] == "UP"
        assert row["correct"] == 1
        assert float(row["realized_pnl"]) > 0
        assert float(row["roi"]) > 0
        assert stats["settled"] == 1
        assert stats["errors"] == 0
        assert stats["last_source"].startswith("event_slug+condition_id:")

        # Idempotent: a second scan finds no OPEN row and cannot double-settle.
        stats2 = await reconciler.reconcile_once()
        assert stats2["settled"] == 1
        assert recorder.open_paper_trades() == []
    finally:
        recorder.close()


@pytest.mark.asyncio
async def test_unresolved_market_stays_open(tmp_path, monkeypatch):
    cfg = _settings(monkeypatch, str(tmp_path / "unresolved.sqlite"))
    recorder = P25ReconcilingPaperRecorder(cfg.db_path, cfg)
    try:
        _insert_open(recorder)
        market = _resolved_market("0xopen")
        market["closed"] = False
        market["automaticallyResolved"] = False
        market["umaResolutionStatus"] = ""
        discovery = FakeDiscovery({"markets": [market]})
        reconciler = PaperTradeReconciler(cfg, discovery, recorder)

        stats = await reconciler.reconcile_once()
        row = recorder.conn.execute(
            "SELECT status FROM paper_trades WHERE condition_id='0xopen'"
        ).fetchone()
        assert row["status"] == "OPEN"
        assert stats["settled"] == 0
        assert stats["unresolved"] == 1
    finally:
        recorder.close()


@pytest.mark.asyncio
async def test_wrong_condition_never_cross_settles(tmp_path, monkeypatch):
    cfg = _settings(monkeypatch, str(tmp_path / "mismatch.sqlite"))
    recorder = P25ReconcilingPaperRecorder(cfg.db_path, cfg)
    try:
        _insert_open(recorder, condition_id="0xwanted")
        discovery = FakeDiscovery(
            {"markets": [_resolved_market("0xother")]},
            fallback=[],
        )
        reconciler = PaperTradeReconciler(cfg, discovery, recorder)

        stats = await reconciler.reconcile_once()
        row = recorder.conn.execute(
            "SELECT status FROM paper_trades WHERE condition_id='0xwanted'"
        ).fetchone()
        assert row["status"] == "OPEN"
        assert stats["settled"] == 0
        assert stats["fetch_empty"] == 1
        assert stats["condition_mismatch"] == 1
    finally:
        recorder.close()


def test_direct_settle_helper_is_idempotent(tmp_path, monkeypatch):
    cfg = _settings(monkeypatch, str(tmp_path / "helper.sqlite"))
    recorder = P25ReconcilingPaperRecorder(cfg.db_path, cfg)
    try:
        _insert_open(recorder, side="DOWN")
        first = recorder.settle_open_paper_condition(
            "0xopen", "DOWN", source="test"
        )
        second = recorder.settle_open_paper_condition(
            "0xopen", "DOWN", source="test"
        )
        assert first == 1
        assert second == 0
        row = recorder.conn.execute(
            "SELECT status, correct FROM paper_trades WHERE condition_id='0xopen'"
        ).fetchone()
        assert row["status"] == "SETTLED"
        assert row["correct"] == 1
    finally:
        recorder.close()
