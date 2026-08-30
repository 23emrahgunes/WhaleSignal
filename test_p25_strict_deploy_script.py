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
    assert "P25_LIVE_MAX_LIMIT_PRICE': '0.255'" in text
    assert "P25_LIVE_ARMED': 'false'" in text
