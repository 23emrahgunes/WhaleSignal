from p25_all5m_web import _main_html, _paper_html


def _assert_all5m_controls(html: str) -> None:
    assert "TÜM 5m DRY TEST" in html
    assert "TÜM 5m CANLIYA GEÇ" in html
    assert "/api/all5m-live/dry" in html
    assert "/api/all5m-live/arm" in html
    assert "/api/all5m-live/disarm" in html
    assert "DirectionEngine-All5m" in html
    assert "post_orders ÇAĞRILMAZ" in html
    assert "BTC/ETH/SOL/XRP 5m" in html
    assert "Önce DRY TEST PASS olmalı" in html
    assert "FAK $1" in html
    assert "ne kadar dolarsa alınır" in html
    assert "PARTIAL FILL" in html
    assert "min depth" in html


def test_main_dashboard_has_dry_then_all5m_live_controls():
    html = _main_html()
    _assert_all5m_controls(html)
    assert "id=\"xrpLiveBtn\"" not in html


def test_paper_records_page_has_dry_then_all5m_live_controls():
    html = _paper_html()
    _assert_all5m_controls(html)
    assert "id=\"xrpLivePaperBtn\"" not in html
