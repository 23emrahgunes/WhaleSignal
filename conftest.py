"""Pytest-only configuration isolation.

Production services intentionally load local dotenv files (for example ``.env`` and
``.env.p3``).  Test runs must not inherit those deployment values: a VPS can have LIVE
or tuned paper settings in its dotenv files while unit tests assert repository defaults.

Pytest imports this file before test collection.  During the test process only, wrap
``BaseSettings.__init__`` so dotenv loading is disabled unless a test explicitly passes
``_env_file`` itself.  Real environment variables remain available, so CI's explicit
PHASE/MODEL_TRAINING_ENABLED overrides still work.  Normal application processes are
unaffected because they do not import ``conftest.py``.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings


_ORIGINAL_BASE_SETTINGS_INIT = BaseSettings.__init__


def _pytest_base_settings_init(self, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003
    kwargs.setdefault("_env_file", None)
    return _ORIGINAL_BASE_SETTINGS_INIT(self, *args, **kwargs)


BaseSettings.__init__ = _pytest_base_settings_init
