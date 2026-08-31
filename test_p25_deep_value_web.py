"""Regression coverage for the P2.5 DEEP_VALUE_WATCH web panel."""
from pathlib import Path

from p25_deep_value_web import enhance_main_html
from p25_web_records import _main_html_with_paper_link


def test_deep_value_box_is_injected_once_into_main_dashboard():
    base = _main_html_with_paper_link()
    html = enhance_main_html(base)
    assert "DİP AVCISI ·" in html
    assert "function deepValueBox(c)" in html
    assert "Canlı ask" in html
    assert "Dip hedefi" in html
    assert "$${stake.toFixed(2)} teorik share" in html
    assert "Full depth / yaş" in html
    assert "Value / min" in html
    assert "🔥 DİP YAKALANDI" in html
    assert "paper_deep_value_max_ask" in html
    assert "paper_deep_value_min_value_multiple" in html
    assert html.count("function deepValueBox(c)") == 1
    assert enhance_main_html(html) == html


def test_deep_mode_replaces_old_checkpoint_paper_line_in_cards():
    html = enhance_main_html(_main_html_with_paper_link())
    assert "${deepValueBox(c)}" in html
    assert "c.paper_entry_mode==='DEEP_VALUE_WATCH'?'':paperLine(c)" in html


def test_runtime_wires_deep_value_plus_all5m_web_and_exposes_dynamic_profile_fields():
    main = Path("p25_main.py").read_text(encoding="utf-8")
    all5m_web = Path("p25_all5m_web.py").read_text(encoding="utf-8")
    engine = Path("p25_deep_value_engine.py").read_text(encoding="utf-8")
    assert "from p25_all5m_web import run_web" in main
    assert "from p25_deep_value_web import enhance_main_html" in all5m_web
    for field in (
        "paper_deep_value_min_ask",
        "paper_deep_value_max_ask",
        "paper_deep_value_stake_usdc",
        "paper_deep_value_slippage",
        "paper_deep_value_min_value_multiple",
    ):
        assert field in engine
