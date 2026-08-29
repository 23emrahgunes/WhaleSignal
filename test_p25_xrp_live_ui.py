from p25_deep_value_web import enhance_main_html
from p25_web import _HTML
from p25_web_records import _control_password, _paper_records_html


def test_main_dashboard_contains_xrp_live_button_and_limits():
    html = enhance_main_html(_HTML)
    assert "XRP 5m CANLIYA GEÇ" in html
    assert "max $1.10" in html
    assert "sapma ≤ %10" in html
    assert "/api/xrp5m-live/" in html
    assert "X-Requested-With" in html


def test_paper_records_page_contains_xrp_live_button_and_limits():
    html = _paper_records_html()
    assert "XRP 5m CANLIYA GEÇ" in html
    assert "max $1.10" in html
    assert "fiyat sapması ≤ %10" in html
    assert "/api/xrp5m-live/status" in html
    assert "P3_WEB_PASSWORD" in html


def test_control_password_prefers_p25_and_falls_back_to_p3(monkeypatch):
    monkeypatch.delenv("P25_LIVE_CONTROL_PASSWORD", raising=False)
    monkeypatch.setenv("P3_WEB_PASSWORD", "p3-operator-secret")
    assert _control_password() == "p3-operator-secret"

    monkeypatch.setenv("P25_LIVE_CONTROL_PASSWORD", "p25-override-secret")
    assert _control_password() == "p25-override-secret"
