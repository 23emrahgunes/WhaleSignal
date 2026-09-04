from pathlib import Path
import shutil
import subprocess

import pytest

import p3_web_dual40 as legacy
from p3_web_dual40_v2 import _ASSET_PATH, externalized_html


def test_dual40_html_has_no_inline_executable_javascript():
    html = externalized_html(legacy._HTML)

    assert f'src="{_ASSET_PATH}?v=2"' in html
    assert '<script src="/assets/dual40-panel.js?v=2" defer></script>' in html
    assert "<script>" not in html
    assert "onclick=" not in html
    assert 'id="probe-btn"' in html
    assert 'id="arm-btn"' in html
    assert 'id="disarm-btn"' in html
    assert 'id="logout-btn"' in html
    assert 'data-refresh-ms="__P3_REFRESH_MS__"' in html


def test_dual40_wrapper_uses_same_origin_script_csp():
    source = Path("p3_web_dual40_v2.py").read_text(encoding="utf-8")

    assert "script-src 'self'" in source
    assert "script-src 'unsafe-inline'" not in source
    assert 'app.router.add_get(_ASSET_PATH, panel_js)' in source
    assert 'content_type="application/javascript"' in source
    assert '"Cache-Control": "no-store, max-age=0"' in source


def test_dual40_panel_script_has_visible_failure_state_and_bound_controls():
    script = Path("p3_dual40_panel.js").read_text(encoding="utf-8")

    assert 'fetchJson("/api/summary")' in script
    assert 'credentials: "same-origin"' in script
    assert 'PANEL HATASI' in script
    assert 'JAVASCRIPT HATASI' in script
    assert 'PROMISE HATASI' in script
    assert 'byId("probe-btn")?.addEventListener' in script
    assert 'byId("arm-btn")?.addEventListener' in script
    assert 'byId("disarm-btn")?.addEventListener' in script
    assert 'byId("logout-btn")?.addEventListener' in script


def test_dual40_panel_javascript_syntax_when_node_is_available():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    result = subprocess.run(
        [node, "--check", "p3_dual40_panel.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
