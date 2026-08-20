"""Tests for the dedicated read-only paper-trade records page/API."""
from __future__ import annotations

import csv
import io

import pytest

from p25_paper_config import PaperSettings
from p25_paper_recorder import P25PaperRecorder
from p25_paper_records import (
    PaperRecordFilters,
    export_paper_records_csv,
    query_paper_records,
)
from p25_paper_records_page import PAPER_RECORDS_HTML
from p25_web_records import _main_html_with_paper_link


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


def _insert(
    recorder: P25PaperRecorder,
    *,
    condition_id: str,
    combo: str,
    attempted_at: float,
    status: str,
    side: str | None = None,
    result: str | None = None,
    correct: int | None = None,
    pnl: float | None = None,
    skip_reason: str | None = None,
) -> None:
    asset, horizon = combo.split(":", 1)
    recorder.conn.execute(
        """
        INSERT INTO paper_trades (
            condition_id, market_id, combo_key, asset, horizon, slug,
            strategy_version, checkpoint_sec, attempted_at, entry_tte_sec,
            side, forecast_p_up, selected_probability, forecast_confidence,
            forecast_grade, forecast_status, forecast_agreement,
            entry_bid, entry_ask, fill_price, forecast_edge, stake_usdc,
            shares, slippage, fee_usdc, status, skip_reason,
            official_result, correct, gross_payout, realized_pnl, roi,
            settled_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            condition_id,
            f"market-{condition_id}",
            combo,
            asset,
            horizon,
            f"{asset.lower()}-updown-{horizon}-{int(attempted_at)}",
            recorder.paper_policy.strategy_version,
            60 if horizon == "5m" else (240 if horizon == "15m" else 600),
            attempted_at,
            60.0,
            side,
            0.70 if side == "UP" else 0.30,
            0.70,
            0.55,
            "MEDIUM",
            "PROVISIONAL",
            0.75,
            0.54,
            0.56,
            0.565,
            0.135,
            2.50,
            4.4248,
            0.005,
            0.0,
            status,
            skip_reason,
            result,
            correct,
            4.4248 if correct else 0.0,
            pnl,
            (pnl / 2.5) if pnl is not None else None,
            attempted_at + 300 if result else None,
        ),
    )
    recorder.conn.commit()


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    cfg = _settings(monkeypatch, str(tmp_path / "paper-records.sqlite"))
    value = P25PaperRecorder(cfg.db_path, cfg)
    _insert(
        value,
        condition_id="btc-settled",
        combo="BTC:5m",
        attempted_at=300.0,
        status="SETTLED",
        side="UP",
        result="UP",
        correct=1,
        pnl=1.9248,
    )
    _insert(
        value,
        condition_id="eth-open",
        combo="ETH:15m",
        attempted_at=200.0,
        status="OPEN",
        side="DOWN",
    )
    _insert(
        value,
        condition_id="sol-skipped",
        combo="SOL:1h",
        attempted_at=100.0,
        status="SKIPPED",
        side="UP",
        skip_reason="LOW_CONFIDENCE",
    )
    try:
        yield value
    finally:
        value.close()


def test_records_are_newest_first_and_paginated(recorder):
    filters = PaperRecordFilters.from_mapping({"limit": "2", "offset": "0"})
    page = query_paper_records(recorder, filters)
    assert page["paperOnly"] is True
    assert page["source"] == "sqlite"
    assert page["pagination"]["total"] == 3
    assert page["pagination"]["has_next"] is True
    assert [row["condition_id"] for row in page["records"]] == [
        "btc-settled",
        "eth-open",
    ]
    assert page["records"][0]["outcome_label"] == "TUTTU"
    assert page["records"][1]["outcome_label"] == "ACIK"


def test_filters_by_asset_horizon_status_and_query(recorder):
    page = query_paper_records(
        recorder,
        PaperRecordFilters.from_mapping(
            {
                "asset": "SOL",
                "horizon": "1h",
                "status": "SKIPPED",
                "q": "LOW_CONFIDENCE",
            }
        ),
    )
    assert page["pagination"]["total"] == 1
    row = page["records"][0]
    assert row["combo_key"] == "SOL:1h"
    assert row["outcome_label"] == "ATLANDI"
    assert row["skip_reason"] == "LOW_CONFIDENCE"


def test_invalid_filter_is_rejected():
    with pytest.raises(ValueError):
        PaperRecordFilters.from_mapping({"asset": "DOGE"})
    with pytest.raises(ValueError):
        PaperRecordFilters.from_mapping({"limit": "1000"})
    with pytest.raises(ValueError):
        PaperRecordFilters.from_mapping({"combo": "BTC:30m"})


def test_csv_export_contains_filtered_rows(recorder):
    content = export_paper_records_csv(
        recorder,
        PaperRecordFilters.from_mapping({"asset": "BTC"}),
    )
    rows = list(csv.DictReader(io.StringIO(content)))
    assert len(rows) == 1
    assert rows[0]["combo_key"] == "BTC:5m"
    assert rows[0]["outcome_label"] == "TUTTU"
    assert rows[0]["strategy_version"] == "RESEARCH_PAPER_V1"


def test_dedicated_page_has_filters_records_and_safety_labels():
    assert "Paper Kayıtları" in PAPER_RECORDS_HTML
    assert "PAPER MODE" in PAPER_RECORDS_HTML
    assert "CANLI İŞLEM KAPALI" in PAPER_RECORDS_HTML
    assert "/api/paper-trades" in PAPER_RECORDS_HTML
    assert "/api/paper-summary" in PAPER_RECORDS_HTML
    assert "/api/paper-trades.csv" in PAPER_RECORDS_HTML
    assert "Kripto Bazlı" in PAPER_RECORDS_HTML
    assert "Market Bazlı Paper Kayıtları" in PAPER_RECORDS_HTML


def test_main_dashboard_gets_paper_records_navigation_link():
    html = _main_html_with_paper_link()
    assert 'href="/paper-trades"' in html
    assert "PAPER KAYITLARI" in html
