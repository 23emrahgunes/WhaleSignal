from pathlib import Path

import p3_dual40_analytics as analytics
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
    assert "P2.6 Readiness" in html
    assert "Market Taraması" in html
    assert "PAPER / LIVE paralellik" in script
    assert "policy.paper_max_concurrent_assets" in script
    assert "policy.live_max_concurrent_assets" in script
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        assert asset in script
    assert "one_global_market_only = true" not in html
    assert "global recovery" not in script
    assert "renderCohorts" in script
    assert "renderP26" in script
    assert "forecast_gate_mode" in script


def test_operational_panel_hides_diagnostic_logs_by_default():
    html = Path("p3_web_dual40.py").read_text(encoding="utf-8")

    assert '<details class="diagnostics">' in html
    assert '<details class="diagnostics" open>' not in html
    assert "Teşhis ve Günlükler" in html
    assert "Teknik tablolar varsayılan olarak gizlidir" in html
    assert html.index("Teşhis ve Günlükler") < html.index("Market Karar Günlüğü")
    assert html.index("Teşhis ve Günlükler") < html.index("DUAL40 Cycle Günlüğü")


def test_operational_panel_prioritizes_asset_lanes_and_market_scan():
    html = Path("p3_web_dual40.py").read_text(encoding="utf-8")
    script = Path("p3_dual40_panel.js").read_text(encoding="utf-8")

    assert 'id="lanes"' in html
    assert "Genel Bakış" in html
    assert "Market Taraması" in html
    assert "lane-grid" in html
    assert 'byId("lanes")' in script
    assert "JSON.stringify(scan.reason_counts" not in script
    assert "JSON.stringify(audit.reason_counts" not in script


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


def test_dual40_analytics_uses_read_only_connection(tmp_path, monkeypatch):
    path = str(tmp_path / "p3.sqlite")
    conn = connect_dual40(path)
    conn.close()

    real_connect = analytics.connect_p3
    modes = []

    def tracked_connect(db_path, *, read_only=False):
        modes.append(read_only)
        return real_connect(db_path, read_only=read_only)

    monkeypatch.setattr(analytics, "connect_p3", tracked_connect)
    build_dual40_summary(path)

    assert modes == [True]
