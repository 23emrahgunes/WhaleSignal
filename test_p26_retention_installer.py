from pathlib import Path


def test_p26_retention_installer_has_bounded_windows_and_timer():
    text = Path("scripts/install_p26_retention.sh").read_text(encoding="utf-8")
    assert "--book-hours 24" in text
    assert "--oracle-hours 72" in text
    assert "--canonical-hours 168" in text
    assert "--health-hours 48" in text
    assert "OnUnitActiveSec=6h" in text
    assert "direction-engine-p26-retention.timer" in text
