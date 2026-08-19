"""Static validation for the one-shot P2.5 deploy script."""

import subprocess
from pathlib import Path


def test_deploy_p25_script_bash_syntax():
    script = Path("deploy_p25.sh")
    assert script.exists()
    result = subprocess.run(
        ["bash", "-n", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_deploy_p25_script_never_deletes_dataset():
    text = Path("deploy_p25.sh").read_text(encoding="utf-8")
    assert "rm -f data/direction_engine.sqlite" not in text
    assert "p25_main.py" in text
    assert "execution_enabled" in text
    assert "live_orders" in text
