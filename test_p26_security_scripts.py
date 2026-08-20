from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HARDEN = ROOT / "scripts" / "harden_port_8091.sh"
ROLLBACK = ROOT / "scripts" / "rollback_port_8091.sh"


def test_security_scripts_bash_syntax():
    for script in (HARDEN, ROLLBACK):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_hardening_defaults_to_dry_run_and_is_scoped(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_iptables = fake_bin / "iptables"
    fake_iptables.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == '-L' || \"$1\" == '-C' ]]; then exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_iptables.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    env["P26_AUTHORIZED_CIDR"] = "203.0.113.4/32"
    result = subprocess.run(
        ["bash", str(HARDEN), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "DRY-RUN:" in result.stdout
    assert "DIRECTION_ENGINE_8091" in result.stdout
    assert "203.0.113.4/32" in result.stdout
    assert "iptables -F INPUT" not in result.stdout
    assert "AWS Security Group remains the primary" in result.stdout


def test_rollback_script_never_flushes_global_input_chain():
    text = ROLLBACK.read_text(encoding="utf-8")
    assert "iptables -F INPUT" not in text
    assert "iptables -X INPUT" not in text
    assert "DIRECTION_ENGINE_8091" in text
