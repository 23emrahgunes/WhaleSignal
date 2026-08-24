"""Regression checks for bounded P2.5 deploy HTTP verification."""
from pathlib import Path


def test_p25_deploy_http_probes_are_bounded_and_health_first():
    text = Path("deploy_p25.sh").read_text(encoding="utf-8")

    assert "wait_http_200()" in text
    assert "--connect-timeout 1" in text
    assert "--max-time 5" in text
    assert "kill -0 \"$new_pid\"" in text

    health = text.index('wait_http_200 "/health"')
    state = text.index('wait_http_200 "/api/state"')
    paper_page = text.index('wait_http_200 "/paper-trades"')
    paper_api = text.index('wait_http_200 "/api/paper-trades?limit=1"')
    paper_summary = text.index('wait_http_200 "/api/paper-summary"')

    assert health < state < paper_page < paper_api < paper_summary


def test_p25_deploy_has_no_unbounded_endpoint_curl():
    text = Path("deploy_p25.sh").read_text(encoding="utf-8")
    endpoint_curls = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("curl ")
    ]
    # All endpoint curls live inside wait_http_200 and therefore include the
    # continuation carrying --max-time on the following lines.
    assert endpoint_curls == ["curl -sS \\"]
