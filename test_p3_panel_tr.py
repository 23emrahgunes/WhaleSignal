from pathlib import Path


def test_p3_panel_is_turkish_and_explains_dry_wallet_safety() -> None:
    text = Path("p3_web.py").read_text(encoding="utf-8")
    assert "P3 Arbitraj Laboratuvarı" in text
    assert "Şu an DRY/SHADOW modundayız" in text
    assert "Cüzdan (DRY)" in text
    assert "İKİ BACAK DOLDU" in text
    assert "LIVE kontrolü" in text
    assert "8094" in text or "live_control_port" in text


def test_p3_main_dashboard_has_no_live_mutation_routes() -> None:
    text = Path("p3_web.py").read_text(encoding="utf-8")
    assert 'web.post("/api/arm"' not in text
    assert 'web.post("/api/disarm"' not in text
    assert '"private_key_loaded": False' in text
    assert '"wallet_loaded": False' in text


def test_p3_control_plane_is_loopback_and_has_explicit_arm() -> None:
    text = Path("p3_live_control.py").read_text(encoding="utf-8")
    assert "loopback-only" in text or "loopback" in text
    assert 'web.post("/api/arm"' in text
    assert 'web.post("/api/disarm"' in text
    assert "X-P3-Control-Token" in text
