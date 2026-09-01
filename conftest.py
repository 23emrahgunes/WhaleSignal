"""Pytest-only configuration isolation.

Production services intentionally load local dotenv files (for example ``.env`` and
``.env.p3``), and operators may also ``source`` those files into their interactive
shell.  Test runs must not inherit either source: a VPS can have LIVE or tuned paper
settings while unit tests assert repository defaults.

Pytest imports this file before test collection.  During the test process only:

* deployment dotenv loading is disabled unless a test explicitly passes ``_env_file``;
* inherited WhaleSignal deployment variables are removed from ``os.environ``;
* the small deterministic CI/deploy override set is preserved.

Normal application processes are unaffected because they do not import
``conftest.py``.
"""
from __future__ import annotations

import os

from pydantic_settings import BaseSettings


# These values are intentionally supplied by CI/deploy when the full suite is run.
# Preserve them even though PAPER_TRADING_ENABLED matches a deployment prefix below.
_PYTEST_INHERITED_ALLOWLIST = {
    "PHASE",
    "MODEL_TRAINING_ENABLED",
    "CALIBRATION_ENABLED",
    "PAPER_TRADING_ENABLED",
}

# Values under these namespaces come from production deployment/runtime config and
# must never change deterministic unit-test defaults merely because the operator
# previously ran `set -a; source .env.p3; source .env; set +a` in the same shell.
_DEPLOYMENT_ENV_PREFIXES = (
    "P3_",
    "P25_",
    "P26_",
    "PAPER_",
    "POLYMARKET_",
    "CLOB_",
)

_DEPLOYMENT_ENV_EXACT = {
    "FORECAST_RECORDING_ENABLED",
    "MODEL_PATH",
    "CALIBRATION_PATH",
    "FEATURE_PRICE_RING_MAX",
    "RESOLUTION_POLL_SEC",
    "WEB_ENABLED",
    "WEB_HOST",
    "WEB_PORT",
}


def _sanitize_inherited_deployment_env() -> None:
    for name in tuple(os.environ):
        if name in _PYTEST_INHERITED_ALLOWLIST:
            continue
        if name in _DEPLOYMENT_ENV_EXACT or name.startswith(_DEPLOYMENT_ENV_PREFIXES):
            os.environ.pop(name, None)


_sanitize_inherited_deployment_env()


_ORIGINAL_BASE_SETTINGS_INIT = BaseSettings.__init__


def _pytest_base_settings_init(self, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003
    kwargs.setdefault("_env_file", None)
    return _ORIGINAL_BASE_SETTINGS_INIT(self, *args, **kwargs)


BaseSettings.__init__ = _pytest_base_settings_init
