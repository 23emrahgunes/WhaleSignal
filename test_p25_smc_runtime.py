from pathlib import Path
from types import SimpleNamespace

from p25_live_all5m_market import All5mMarketBuyController
from p25_smc_runtime import (
    ResilientAll5mMarketBuyController,
    _refs_by_asset,
    _trim_sorted_prices,
)


def _ref(asset: str):
    return SimpleNamespace(
        combo=SimpleNamespace(
            asset=SimpleNamespace(value=asset),
            horizon=SimpleNamespace(value="5m"),
        ),
        slug=f"{asset.lower()}-updown-5m-test",
    )


def test_trim_sorted_prices_keeps_only_required_tail():
    rows = [(index * 100, 100.0 + index) for index in range(100)]
    trimmed = _trim_sorted_prices(rows, now_ms=9_900, lookback_ms=1_000)
    assert trimmed[0][0] == 8_900
    assert trimmed[-1][0] == 9_900
    assert len(trimmed) == 11


def test_refs_by_asset_ignores_non_5m_and_deduplicates():
    btc = _ref("BTC")
    duplicate = _ref("BTC")
    eth15 = SimpleNamespace(
        combo=SimpleNamespace(
            asset=SimpleNamespace(value="ETH"),
            horizon=SimpleNamespace(value="15m"),
        )
    )
    found = _refs_by_asset([btc, duplicate, eth15])
    assert found == {"BTC": btc}


def test_resilient_dry_reacquires_missing_sol_before_base_probe(monkeypatch):
    controller = object.__new__(ResilientAll5mMarketBuyController)
    controller.cfg = SimpleNamespace(gamma_host="https://gamma.invalid")
    monkeypatch.setenv("P25_DRY_MARKET_WAIT_SEC", "0.2")

    recovered = _ref("SOL")

    def acquire(asset, *, timeout):
        assert timeout > 0
        return recovered if asset == "SOL" else None

    controller._current_ref_from_gamma = acquire
    captured = {}

    def base_probe(self, refs):
        captured["assets"] = sorted(_refs_by_asset(refs))
        return {"ok": True, "checks": {}}

    monkeypatch.setattr(All5mMarketBuyController, "dry_probe", base_probe)

    result = ResilientAll5mMarketBuyController.dry_probe(
        controller,
        [_ref("BTC"), _ref("ETH"), _ref("XRP")],
    )

    assert result["ok"] is True
    assert captured["assets"] == ["BTC", "ETH", "SOL", "XRP"]
    acquisition = result["checks"]["market_acquisition"]
    assert acquisition["initially_missing"] == ["SOL"]
    assert acquisition["reacquired"] == ["SOL"]
    assert acquisition["still_missing"] == []
    assert acquisition["fail_closed"] is True


def test_smc_entrypoint_installs_runtime_before_smc_patch():
    text = Path("p25_main_smc.py").read_text(encoding="utf-8")
    runtime = text.index("install_smc_v3_runtime_hardening()")
    structural = text.index("enable_smc_v3()")
    assert runtime < structural

    runtime_text = Path("p25_smc_runtime.py").read_text(encoding="utf-8")
    assert 'os.environ["HORIZONS"] = "5m"' in runtime_text
    assert "Transient Gamma miss; retained still-current 5m refs" in runtime_text
    assert "P25_DRY_MARKET_WAIT_SEC" in runtime_text
