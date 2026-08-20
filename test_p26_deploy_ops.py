"""Static and shell-syntax tests for P2.6 AWS operations scripts."""
from __future__ import annotations

import subprocess
from pathlib import Path


SCRIPTS = [
    Path("deploy_p26.sh"),
    Path("scripts/status_p26.sh"),
    Path("scripts/stop_p26.sh"),
    Path("scripts/harden_port_8091.sh"),
    Path("scripts/rollback_port_8091.sh"),
]


def test_all_p26_operations_scripts_have_valid_bash_syntax():
    for script in SCRIPTS:
        assert script.exists(), script
        result = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_deploy_help_has_no_side_effects():
    result = subprocess.run(
        ["bash", "deploy_p26.sh", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "P2.5" in result.stdout
    assert "Network hardening is intentionally NOT applied" in result.stdout


def test_deploy_is_fail_closed_and_does_not_replace_p25():
    text = Path("deploy_p26.sh").read_text(encoding="utf-8")
    forbidden = (
        "rm -f data/direction_engine.sqlite",
        "rm data/direction_engine.sqlite",
        "pkill -f",
        "systemctl stop direction-engine-p25",
        "systemctl disable direction-engine-p25",
        "p25_main.py >",
        "PRIVATE_KEY=",
        "private_key =",
        "submit_order(",
        "create_order(",
    )
    for token in forbidden:
        assert token not in text
    assert "p26_baseline_freeze.py freeze" in text
    assert "p26_baseline_freeze.py verify" in text
    assert "direction-engine-p26-oracle.service" in text
    assert "direction-engine-p26-dataset.service" in text
    assert "no RTDS oracle tick persisted within 120 seconds" in text
    assert 'harden_port_8091.sh" --dry-run' in text
    assert 'harden_port_8091.sh" --apply' not in text


def test_deploy_generates_dynamic_service_identity_and_paths():
    text = Path("deploy_p26.sh").read_text(encoding="utf-8")
    assert "stat -c '%U'" in text
    assert "WorkingDirectory=$REPO_DIR" in text
    assert "ExecStart=$PY $REPO_DIR/p26_oracle_daemon.py" in text
    assert "ExecStart=$PY $REPO_DIR/p26_dataset_daemon.py" in text
    assert "User=ubuntu" not in text
    assert "WorkingDirectory=/home/ubuntu" not in text


def test_redeploy_restarts_existing_sidecars_instead_of_only_enabling_them():
    text = Path("deploy_p26.sh").read_text(encoding="utf-8")
    assert "systemctl enable direction-engine-p26-oracle.service" in text
    assert "systemctl enable direction-engine-p26-dataset.service" in text
    assert "systemctl restart direction-engine-p26-oracle.service" in text
    assert "systemctl restart direction-engine-p26-dataset.service" in text
    assert "systemctl enable --now direction-engine-p26-oracle.service" not in text
    assert "systemctl enable --now direction-engine-p26-dataset.service" not in text


def test_stop_script_preserves_all_databases_and_p25_runtime():
    text = Path("scripts/stop_p26.sh").read_text(encoding="utf-8")
    assert "direction-engine-p26-dataset.service" in text
    assert "direction-engine-p26-oracle.service" in text
    assert "direction-engine-p25" not in text
    assert "direction_engine.sqlite" not in text
    assert "p26_research.sqlite was preserved" in text
    assert "rollback_port_8091" not in text


def test_p26_runtime_files_are_gitignored():
    entries = set(Path(".gitignore").read_text(encoding="utf-8").splitlines())
    assert ".env.p26" in entries
    assert "logs/" in entries
    assert "reports/" in entries
    assert "data/" in entries
    assert "models/" in entries
