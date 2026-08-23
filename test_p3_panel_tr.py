from pathlib import Path


def test_p3_panel_is_turkish_and_explains_dry_wallet_safety() -> None:
    text = Path("p3_web.py").read_text(encoding="utf-8")
    assert "P3 Arbitraj Laboratuvarı" in text
    assert "Şu an DRY/SHADOW modundayız" in text
    assert "Cüzdan (DRY)" in text
    assert "İKİ BACAK DOLDU" in text
    assert "LIVE kontrolü" in text
    assert "LIVE Kontrol — 8093" in text
    assert "BAĞLANTI / KİMLİK TESTİ (EMİR YOK)" in text
    assert "CANLIYA GEÇ" in text
    assert "DRY'A DÖN" in text
    assert "8094" not in text


def test_p3_8093_has_authenticated_csrf_protected_live_routes() -> None:
    text = Path("p3_web.py").read_text(encoding="utf-8")
    auth = Path("p3_web_auth.py").read_text(encoding="utf-8")
    assert 'web.post("/api/live/probe"' in text
    assert 'web.post("/api/live/arm"' in text
    assert 'web.post("/api/live/disarm"' in text
    assert "X-P3-CSRF" in text
    assert "httponly=True" in text
    assert 'samesite="Strict"' in text
    assert "compare_digest" in auth
    assert "web_login_max_failures" in auth
    assert "X-P3-Control-Token" not in text
    assert '"private_key_loaded": False' in text
    assert '"wallet_loaded": False' in text


def test_p3_daemon_does_not_start_separate_8094_control_server() -> None:
    daemon = Path("p3_daemon.py").read_text(encoding="utf-8")
    assert "run_live_control" not in daemon
    assert "authenticated_web_8093" not in daemon  # log wording stays simple
    assert "control=web8093" in daemon


def test_health_is_minimal_but_operational_api_is_protected_by_middleware() -> None:
    text = Path("p3_web.py").read_text(encoding="utf-8")
    assert 'public = request.path in {"/health", "/login"}' in text
    assert '"error": "AUTH_REQUIRED"' in text
    assert 'web.get("/health", health)' in text
    assert 'web.get("/api/summary", summary)' in text
