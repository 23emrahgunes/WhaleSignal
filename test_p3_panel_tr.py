from pathlib import Path


def test_p3_panel_is_turkish_and_explains_dry_wallet_safety() -> None:
    text = Path("p3_web.py").read_text(encoding="utf-8")
    assert "P3 Arbitraj Laboratuvarı" in text
    assert "Şu an DRY/SHADOW modundayız" in text
    assert "Cüzdan (DRY)" in text
    assert "GERÇEK EMİR YOK" in text
    assert "İki bacak doldu" in text or "İKİ BACAK DOLDU" in text


def test_p3_dry_api_explicitly_requires_no_wallet_or_private_key() -> None:
    text = Path("p3_web.py").read_text(encoding="utf-8")
    assert '"wallet_required": False' in text
    assert '"wallet_loaded": False' in text
    assert '"private_key_loaded": False' in text
    assert '"signing_enabled": False' in text
    assert '"order_submission_enabled": False' in text
