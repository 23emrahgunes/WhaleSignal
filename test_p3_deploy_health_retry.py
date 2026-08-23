from pathlib import Path


def test_p3_deploy_does_not_depend_on_p25_http_responsiveness() -> None:
    text = Path("deploy_p3.sh").read_text(encoding="utf-8")
    assert "p25_alive()" in text
    assert "pgrep -f 'p25_main\\.py'" in text
    assert "127.0.0.1:8091/health" not in text
    assert "direction-engine-p26-book.service" in text
    assert "direction-engine-p26-dataset.service" in text
    assert "direction-engine-p26-oracle.service" in text


def test_p3_deploy_installs_live_sdk_only_when_feature_enabled() -> None:
    text = Path("deploy_p3.sh").read_text(encoding="utf-8")
    assert "requirements-live.txt" in text
    assert "live_feature_enabled" in text
    assert "P3 ARBITRAGE DEPLOY PASS | starts=DRY" in text
    assert "control=authenticated_8093" in text


def test_p3_smoke_requires_dry_start_and_authenticated_8093_boundary() -> None:
    text = Path("scripts/smoke_p3.sh").read_text(encoding="utf-8")
    assert "wait_http_200()" in text
    assert "wait_http_200 p3 http://127.0.0.1:8093/health" in text
    assert "127.0.0.1:8091/health" not in text
    assert "pgrep -f 'p25_main\\.py'" in text
    assert "assert health['mode'] == 'DRY'" in text
    assert "execution_enabled'] is False" in text
    assert "P3_8093_AUTH_BOUNDARY_SMOKE_PASS health=200 protected_api=401" in text
    assert "http://127.0.0.1:8093/api/summary" in text
    assert '[[ "$AUTH_CODE" == "401" ]]' in text