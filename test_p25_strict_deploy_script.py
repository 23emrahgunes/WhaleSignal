import subprocess
from pathlib import Path


def test_strict_deploy_script_bash_syntax():
    script = Path("deploy_p25_strict.sh")
    assert script.exists()
    result = subprocess.run(
        ["bash", "-n", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_strict_deploy_profile_contains_directional_v2_contract():
    text = Path("deploy_p25_strict.sh").read_text(encoding="utf-8")
    assert "INDEP_PTB_BINANCE_DIRECTIONAL_5M_V2" in text
    assert "PAPER_DEEP_VALUE_ENTRY_TTE_MAX_SEC': '75'" in text
    assert "PAPER_INDEPENDENT_DEADZONE_LOW': '0.33'" in text
    assert "PAPER_INDEPENDENT_DEADZONE_HIGH': '0.67'" in text
    assert "PAPER_STRICT_MAX_FLIP_RATE': '0.68'" in text
    assert "PAPER_STRICT_STABILITY_SEC': '3.0'" in text
    assert "PAPER_DEEP_VALUE_MAX_BOOK_AGE_MS': '750'" in text
    assert "PAPER_DEEP_VALUE_MIN_DEPTH_MULTIPLE': '1.50'" in text
    assert "PAPER_DEEP_VALUE_MIN_ASK': '0.05'" in text
    assert "PAPER_DEEP_VALUE_MAX_ASK': '0.75'" in text
    assert "PAPER_MIN_EDGE': '0.08'" in text
    assert "PAPER_DEEP_VALUE_MIN_VALUE_MULTIPLE': '1.12'" in text
    assert "P25_LIVE_MAX_LIMIT_PRICE': '0.83'" in text
    assert "P25_LIVE_MAX_STAKE_USDC': '1.10'" in text
    assert "P25_LIVE_ARMED': 'false'" in text
    assert "ALL5M LIVE=DRY_REQUIRED+UNARMED" in text
    assert "SIGNAL_IMMEDIATE_FAK_LIVE_EDGE_CAP" in text
    assert "SIGNAL_IMMEDIATE_LIMIT_CAP" in text
    assert "paper_drift_enforced" in text
    assert "live_min_edge" in text
    assert "pre_submit_book_check" in text
    assert "matching_engine_is_liquidity_gate" in text
    assert "parallel_execution" in text
    assert "max_parallel_workers" in text
    assert "partial_fill_ok" in text
    assert "fak_no_match_is_normal" in text


def test_strict_deploy_is_transactional_and_never_runs_baseline_deploy():
    text = Path("deploy_p25_strict.sh").read_text(encoding="utf-8")
    assert "bash ./deploy_p25.sh" not in text
    assert "STRICT PRECHECK: TESTS" in text
    assert "CANDIDATE CONFIG PASS" in text
    assert "ACTIVATE DIRECTIONAL EDGE V2 (atomic)" in text
    assert "ROLLBACK: previous profile restarted" in text
    assert text.index("STRICT PRECHECK: TESTS") < text.index("ACTIVATE DIRECTIONAL EDGE V2 (atomic)")
    assert text.index("CANDIDATE CONFIG PASS") < text.index("pkill -f 'python.*p25_main.py'")


def test_strict_deploy_success_banner_is_safe_under_nounset_without_positional_args():
    text = Path("deploy_p25_strict.sh").read_text(encoding="utf-8")
    banner = next(
        line for line in text.splitlines()
        if "DIRECTIONAL EDGE V2 DEPLOY PASS" in line
    )
    result = subprocess.run(
        ["bash", "-u", "-c", banner],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "transactional=true" in result.stdout
    assert "order=FAK-$1@SIGNAL_LIMIT" in result.stdout
    assert "paper_drift=OFF" in result.stdout
    assert "book_precheck=OFF" in result.stdout
    assert "live_edge>=8pt" in result.stdout
    assert "parallel=4" in result.stdout
    assert "max=$1.10/order" in result.stdout
