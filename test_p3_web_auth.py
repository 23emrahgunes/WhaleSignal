from __future__ import annotations

import re

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from p3_config import P3Settings
from p3_live_state import LiveState, MODE_DRY, MODE_LIVE_ARMED
from p3_web import build_web_app
from p3_web_auth import AuthenticationError, LoginRateLimited, WebAuthManager


def _settings(tmp_path, **overrides) -> P3Settings:
    values = {
        "p3_db_path": str(tmp_path / "p3.sqlite"),
        "p26_db_path": str(tmp_path / "p26.sqlite"),
        "live_feature_enabled": True,
        "live_auto_execute_enabled": True,
        "live_require_dry_validated": False,
        "web_auth_required": True,
        "web_username": "operator",
        "web_password": "integration-pass-12345",
        "web_cookie_secure": False,
        "web_host": "127.0.0.1",
        "web_port": 18093,
    }
    values.update(overrides)
    settings = P3Settings(**values)
    settings.validate_research_safety()
    return settings


def _ok_preflight(_settings, *, for_arming: bool):
    return {
        "ok": True,
        "purpose": "ARM_LIVE" if for_arming else "CONNECTIVITY_ONLY_NO_ORDER",
        "checked_at_ms": 123,
        "reasons": [],
        "warnings": [],
        "checks": {},
        "risk": {},
    }


@pytest.mark.asyncio
async def test_8093_login_csrf_probe_arm_logout_flow(tmp_path) -> None:
    settings = _settings(tmp_path)
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)
    app = build_web_app(settings, live_state=state, preflight_fn=_ok_preflight)
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        health = await client.get("/health")
        assert health.status == 200
        health_json = await health.json()
        assert health_json["mode"] == MODE_DRY
        assert "private_key_loaded" not in health_json
        assert "wallet_loaded" not in health_json

        unauth = await client.get("/api/summary")
        assert unauth.status == 401
        assert (await unauth.json())["error"] == "AUTH_REQUIRED"

        bad = await client.post(
            "/login",
            data={"username": "operator", "password": "wrong-password"},
            allow_redirects=False,
        )
        assert bad.status == 401

        login = await client.post(
            "/login",
            data={"username": "operator", "password": "integration-pass-12345"},
            allow_redirects=False,
        )
        assert login.status == 303
        cookie_header = login.headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie_header
        assert "SameSite=Strict" in cookie_header
        assert "integration-pass-12345" not in cookie_header

        page = await client.get("/")
        assert page.status == 200
        html = await page.text()
        assert "integration-pass-12345" not in html
        match = re.search(r'<meta name="p3-csrf" content="([^"]+)">', html)
        assert match is not None
        csrf = match.group(1)
        assert csrf

        rejected = await client.post("/api/live/probe")
        assert rejected.status == 403
        assert (await rejected.json())["error"] == "CSRF_REJECTED"

        probe = await client.post("/api/live/probe", headers={"X-P3-CSRF": csrf})
        assert probe.status == 200
        assert (await probe.json())["purpose"] == "CONNECTIVITY_ONLY_NO_ORDER"
        assert state.snapshot().mode == MODE_DRY

        arm = await client.post("/api/live/arm", headers={"X-P3-CSRF": csrf})
        assert arm.status == 200
        assert (await arm.json())["armed"] is True
        assert state.snapshot().mode == MODE_LIVE_ARMED

        logout = await client.post("/logout", headers={"X-P3-CSRF": csrf})
        assert logout.status == 200
        assert state.snapshot().mode == MODE_DRY

        after = await client.get("/api/summary")
        assert after.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_arm_stays_dry_and_returns_preflight(tmp_path) -> None:
    settings = _settings(tmp_path)
    state = LiveState(live_feature_enabled=True, auto_execute_enabled=True)

    def fail_preflight(_settings, *, for_arming: bool):
        return {
            "ok": not for_arming,
            "purpose": "ARM_LIVE" if for_arming else "CONNECTIVITY_ONLY_NO_ORDER",
            "checked_at_ms": 123,
            "reasons": ["INSUFFICIENT_COLLATERAL"] if for_arming else [],
            "warnings": [],
            "checks": {},
            "risk": {},
        }

    app = build_web_app(settings, live_state=state, preflight_fn=fail_preflight)
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    try:
        await client.post(
            "/login",
            data={"username": "operator", "password": "integration-pass-12345"},
            allow_redirects=False,
        )
        html = await (await client.get("/")).text()
        csrf = re.search(r'<meta name="p3-csrf" content="([^"]+)">', html).group(1)
        response = await client.post("/api/live/arm", headers={"X-P3-CSRF": csrf})
        assert response.status == 409
        body = await response.json()
        assert body["armed"] is False
        assert "INSUFFICIENT_COLLATERAL" in body["preflight"]["reasons"]
        assert state.snapshot().mode == MODE_DRY
    finally:
        await client.close()


def test_auth_manager_rate_limits_failed_logins(tmp_path) -> None:
    settings = _settings(tmp_path, web_login_max_failures=2, web_login_window_sec=60)
    now = [100.0]
    auth = WebAuthManager(settings, clock=lambda: now[0])
    with pytest.raises(AuthenticationError):
        auth.authenticate("operator", "bad-1", remote="1.2.3.4")
    with pytest.raises(AuthenticationError):
        auth.authenticate("operator", "bad-2", remote="1.2.3.4")
    with pytest.raises(LoginRateLimited):
        auth.authenticate("operator", "integration-pass-12345", remote="1.2.3.4")

    now[0] += 61
    session = auth.authenticate("operator", "integration-pass-12345", remote="1.2.3.4")
    assert session.token
    assert session.csrf_token
