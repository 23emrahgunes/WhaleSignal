from pathlib import Path


def test_p3_deploy_retries_transient_p25_health() -> None:
    text = Path("deploy_p3.sh").read_text(encoding="utf-8")
    assert "wait_http_200()" in text
    assert "p25-pre" in text
    assert "p25-post" in text
    assert "http://127.0.0.1:8091/health" in text
    assert " 30)" in text


def test_p3_smoke_retries_both_p25_and_p3_health() -> None:
    text = Path("scripts/smoke_p3.sh").read_text(encoding="utf-8")
    assert "wait_http_200()" in text
    assert "wait_http_200 p25 http://127.0.0.1:8091/health" in text
    assert "wait_http_200 p3 http://127.0.0.1:8093/health" in text
    assert "P3_AWS_SMOKE_PASS p25=200 p3=200" in text
