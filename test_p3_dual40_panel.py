from pathlib import Path

from p3_dual40_analytics import build_dual40_summary
from p3_dual40_store import (
    connect_dual40,
    create_cycle,
    update_cycle,
    upsert_market_decision,
)


def test_panel_contains_asset_lane_state():
    html = Path("p3_web_dual40.py").read_text(encoding="utf-8")
    script = Path("p3_dual40_panel.js").read_text(encoding="utf-8")

    assert "Market Karar Günlüğü" in html
    assert "Gate Kohortları" in html
    assert "P2.6 Tahmin Paper Readiness" in html
    assert "Opening Reason" in html
    assert "PAPER paralellik" in script
    assert "LIVE paralellik" in script
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        assert asset in script
    assert "one_global_market_only = true" not in html
    assert "global recovery" not in script
    assert "renderCohorts" in script
    assert "renderP26" in script
    assert "forecast_gate_mode" in script


def test_asset_panel_metrics_keep_paper_and_live_scopes_separate(tmp_path):
    path = str(tmp_path / "p3.sqlite")
    conn = connect_dual40(path)
    try:
        for scope, pnl in (("PAPER", 1.0), ("LIVE", -5.0)):
            cycle_id = create_cycle(
                conn,
                scope=scope,
                asset="BTC",
                session_id=None,
                condition_id=f"{scope.lower()}-btc",
                combo_key="BTC:5m",
                market_end_ts_ms=1_000,
                level_index=0,
                target_shares=5.0,
                maker_price=0.40,
                status="NO_FILL",
                gate={},
                up_token_id=f"{scope}-up",
                down_token_id=f"{scope}-down",
                loss_pool_before_usdc=0.0,
            )
            update_cycle(
                conn,
                cycle_id,
                realized_pnl_usdc=pnl,
                resolved_at_ms=2_000,
            )
            upsert_market_decision(
                conn,
                scope=scope,
                asset="BTC",
                combo_key="BTC:5m",
                condition_id=f"{scope.lower()}-btc",
                market_start_ts_ms=0,
                market_end_ts_ms=1_000,
                decision="REJECTED_TEST",
                reason=f"{scope}_ONLY",
            )
    finally:
        conn.close()

    btc = build_dual40_summary(path)["by_asset"]["BTC"]
    assert btc["realized_pnl_usdc"] == 1.0
    assert btc["markets_seen"] == 1
    assert btc["performance"]["PAPER"]["realized_pnl_usdc"] == 1.0
    assert btc["performance"]["LIVE"]["realized_pnl_usdc"] == -5.0
