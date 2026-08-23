from pathlib import Path


def test_p3_deploy_does_not_depend_on_p25_http_responsiveness() -> None:
    text = Path("deploy_p3.sh").read_text(encoding="utf-8")
    assert "p25_alive()" in text
    assert "pgrep -f 'p25_main\\.py'" in text
    assert "127.0.0.1:8091/health" not in text
    assert "direction-engine-p26-book.service" in text
    assert "direction-engine-p26-dataset.service" in text
    assert "direction-engine-p26-oracle.service" in text


def test_p3_smoke_requires_p3_http_and_p25_process_only() -> None:
    text = Path("scripts/smoke_p3.sh").read_text(encoding="utf-8")
    assert "wait_http_200()" in text
    assert "wait_http_200 p3 http://127.0.0.1:8093/health" in text
    assert "127.0.0.1:8091/health" not in text
    assert "pgrep -f 'p25_main\\.py'" in text
    assert "P3_AWS_SMOKE_PASS p25_process=true p3=200" in text
