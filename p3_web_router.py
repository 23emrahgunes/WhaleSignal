"""Select the P3 dashboard implementation for the active strategy mode."""
from __future__ import annotations

import asyncio
from typing import Any

from p3_config import P3Settings
from p3_live_state import LiveState
from p3_web import run_web as run_structural_web
from p3_web_dual40_v2 import run_web as run_dual40_web


async def run_web(
    settings: P3Settings,
    stop: asyncio.Event,
    *,
    live_state: LiveState | None = None,
    dual40_engine: Any | None = None,
) -> None:
    if settings.dual40_active:
        await run_dual40_web(
            settings,
            stop,
            live_state=live_state,
            dual40_engine=dual40_engine,
        )
        return
    await run_structural_web(settings, stop, live_state=live_state)
