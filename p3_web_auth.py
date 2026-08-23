"""In-memory authentication for the P3 8093 operator dashboard.

No session, CSRF token, username or password is persisted to SQLite. Process restart
invalidates every browser session, matching the P3 rule that restart also resets LIVE
to DRY. Login failures are rate-limited per remote address.
"""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import secrets
import time
from typing import Any

from p3_config import P3Settings

SESSION_COOKIE = "p3_operator_session"


class AuthenticationError(ValueError):
    pass


class LoginRateLimited(AuthenticationError):
    pass


@dataclass(frozen=True)
class OperatorSession:
    token: str
    csrf_token: str
    created_at: float
    expires_at: float


class WebAuthManager:
    def __init__(self, settings: P3Settings, *, clock=time.monotonic) -> None:
        self.settings = settings
        self.clock = clock
        self._sessions: dict[str, OperatorSession] = {}
        self._failures: dict[str, list[float]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.web_auth_required)

    def _now(self) -> float:
        return float(self.clock())

    def _cleanup_sessions(self) -> None:
        now = self._now()
        expired = [token for token, session in self._sessions.items() if session.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)

    def _recent_failures(self, remote: str) -> list[float]:
        now = self._now()
        cutoff = now - float(self.settings.web_login_window_sec)
        recent = [ts for ts in self._failures.get(remote, []) if ts >= cutoff]
        if recent:
            self._failures[remote] = recent
        else:
            self._failures.pop(remote, None)
        return recent

    def is_rate_limited(self, remote: str) -> bool:
        return len(self._recent_failures(remote)) >= int(self.settings.web_login_max_failures)

    def authenticate(self, username: str, password: str, *, remote: str) -> OperatorSession:
        remote_key = str(remote or "unknown")
        if not self.enabled:
            raise AuthenticationError("web authentication is disabled")
        if self.is_rate_limited(remote_key):
            raise LoginRateLimited("too many failed login attempts")

        # compare_digest is used for both fields. The password is read from SecretStr
        # only for this comparison and is never inserted into a response or log.
        user_ok = hmac.compare_digest(str(username), str(self.settings.web_username))
        pass_ok = hmac.compare_digest(str(password), self.settings.web_password_value())
        if not (user_ok and pass_ok):
            self._failures.setdefault(remote_key, []).append(self._now())
            raise AuthenticationError("invalid credentials")

        self._failures.pop(remote_key, None)
        now = self._now()
        session = OperatorSession(
            token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(24),
            created_at=now,
            expires_at=now + float(self.settings.web_session_ttl_sec),
        )
        self._sessions[session.token] = session
        return session

    def session_from_token(self, token: str | None) -> OperatorSession | None:
        if not self.enabled:
            return None
        self._cleanup_sessions()
        if not token:
            return None
        return self._sessions.get(str(token))

    def session_from_request(self, request: Any) -> OperatorSession | None:
        return self.session_from_token(request.cookies.get(SESSION_COOKIE))

    def validate_csrf(self, session: OperatorSession | None, supplied: str | None) -> bool:
        if session is None or not supplied:
            return False
        return hmac.compare_digest(session.csrf_token, str(supplied))

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(str(token), None)

    def revoke_request(self, request: Any) -> None:
        self.revoke(request.cookies.get(SESSION_COOKIE))

    def public_session(self, session: OperatorSession) -> dict[str, Any]:
        remaining = max(0, int(session.expires_at - self._now()))
        return {
            "authenticated": True,
            "username": self.settings.web_username,
            "expires_in_sec": remaining,
        }
