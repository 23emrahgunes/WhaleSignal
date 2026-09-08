"""Read-only P2.5 forecast adapter for DUAL40 directional vetoes."""
from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable


def _condition_suffix(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized[-8:] if normalized else ""


@dataclass(frozen=True)
class ForecastGateDecision:
    eligible: bool
    reason: str
    available: bool
    p_up_external: float | None = None
    confidence: float | None = None
    model_version: str | None = None
    age_ms: int | None = None
    combo_key: str | None = None
    condition_suffix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_forecast_value(
    *,
    p_up_external: float | None,
    confidence: float | None,
    model_version: str | None,
    age_ms: int | None,
    max_age_ms: int,
    minimum_p_up: float,
    maximum_p_up: float,
    combo_key: str,
    condition_suffix: str,
) -> ForecastGateDecision:
    try:
        probability = float(p_up_external) if p_up_external is not None else None
    except (TypeError, ValueError):
        probability = None
    if probability is None or not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        return ForecastGateDecision(False, "FORECAST_VALUE_MISSING", False)
    if not model_version:
        return ForecastGateDecision(False, "FORECAST_MODEL_VERSION_MISSING", False)
    if age_ms is None or int(age_ms) < 0 or int(age_ms) > int(max_age_ms):
        return ForecastGateDecision(
            False,
            "FORECAST_STALE",
            False,
            probability,
            confidence,
            model_version,
            age_ms,
            combo_key,
            condition_suffix,
        )
    allowed = float(minimum_p_up) <= probability <= float(maximum_p_up)
    return ForecastGateDecision(
        allowed,
        "FORECAST_NEUTRAL" if allowed else "REJECTED_STRONG_DIRECTIONAL_ALPHA",
        True,
        probability,
        confidence,
        model_version,
        int(age_ms),
        combo_key,
        condition_suffix,
    )


class P25StateForecastProvider:
    """Fetch one local P2.5 state snapshot and match cards without model mutation."""

    def __init__(
        self,
        *,
        state_url: str,
        timeout_ms: int,
        max_age_ms: int,
        cache_ms: int,
        tte_tolerance_sec: float,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.state_url = str(state_url)
        self.timeout_ms = int(timeout_ms)
        self.max_age_ms = int(max_age_ms)
        self.cache_ms = int(cache_ms)
        self.tte_tolerance_sec = float(tte_tolerance_sec)
        self.opener = opener
        self._payload: dict[str, Any] | None = None
        self._fetched_at_ms: int | None = None
        self._last_attempt_ms: int | None = None
        self._error_reason: str | None = None

    def prefetch(self, now_ms: int) -> None:
        now = int(now_ms)
        if (
            self._last_attempt_ms is not None
            and now - self._last_attempt_ms < self.cache_ms
        ):
            return
        self._last_attempt_ms = now
        request = urllib.request.Request(
            self.state_url,
            headers={"Accept": "application/json", "User-Agent": "WhaleSignal-DUAL40/1"},
        )
        try:
            with self.opener(request, timeout=self.timeout_ms / 1000.0) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("forecast response too large")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
                raise ValueError("forecast response schema mismatch")
            self._payload = payload
            self._fetched_at_ms = now
            self._error_reason = None
        except Exception as exc:  # noqa: BLE001
            self._payload = None
            self._fetched_at_ms = None
            self._error_reason = f"FORECAST_PROVIDER_ERROR:{type(exc).__name__}"

    @staticmethod
    def _payload_age_ms(payload: dict[str, Any], now_ms: int) -> int | None:
        value = payload.get("now")
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(timestamp) or timestamp <= 0:
            return None
        timestamp_ms = (
            int(timestamp * 1000.0)
            if timestamp < 10_000_000_000
            else int(timestamp)
        )
        return max(0, int(now_ms) - timestamp_ms)

    @staticmethod
    def _card_age_ms(
        card: dict[str, Any],
        *,
        fetch_age_ms: int,
        payload_age_ms: int | None,
    ) -> int | None:
        ages: list[int] = []
        for key in ("clob_age_ms", "transport_age_ms", "source_age_ms", "book_age_ms"):
            value = card.get(key)
            if value is None:
                continue
            try:
                ages.append(max(0, int(float(value))))
            except (TypeError, ValueError):
                return None
        if not ages:
            return None
        elapsed = max(
            max(0, int(fetch_age_ms)),
            max(0, int(payload_age_ms)) if payload_age_ms is not None else 0,
        )
        return max(ages) + elapsed

    def evaluate(
        self,
        *,
        now_ms: int,
        combo_key: str,
        condition_id: str,
        tte_sec: float,
        minimum_p_up: float,
        maximum_p_up: float,
    ) -> ForecastGateDecision:
        self.prefetch(now_ms)
        if self._payload is None or self._fetched_at_ms is None:
            return ForecastGateDecision(
                False,
                self._error_reason or "FORECAST_MISSING",
                False,
            )
        cards = [
            card
            for card in self._payload.get("cards", [])
            if isinstance(card, dict)
            and bool(card.get("active"))
            and str(card.get("combo") or "").upper() == str(combo_key).upper()
        ]
        if not cards:
            return ForecastGateDecision(False, "FORECAST_CARD_MISSING", False)
        condition_suffix = _condition_suffix(condition_id)
        matching = (
            [
                card
                for card in cards
                if _condition_suffix(card.get("condition_id")) == condition_suffix
            ]
            if condition_suffix
            else []
        )
        if not matching:
            return ForecastGateDecision(False, "FORECAST_MARKET_MISMATCH", False)
        card = min(
            matching,
            key=lambda item: abs(float(item.get("tte_sec") or -10_000.0) - float(tte_sec)),
        )
        try:
            card_tte = float(card.get("tte_sec"))
        except (TypeError, ValueError):
            return ForecastGateDecision(False, "FORECAST_TTE_MISSING", False)
        fetch_age = max(0, int(now_ms) - self._fetched_at_ms)
        payload_age = self._payload_age_ms(self._payload, int(now_ms))
        elapsed_ms = max(fetch_age, payload_age or 0)
        adjusted_card_tte = max(0.0, card_tte - elapsed_ms / 1000.0)
        if abs(adjusted_card_tte - float(tte_sec)) > self.tte_tolerance_sec:
            return ForecastGateDecision(False, "FORECAST_MARKET_MISMATCH", False)
        if not bool(card.get("prediction_ready")):
            return ForecastGateDecision(False, "FORECAST_NOT_READY", False)
        age_ms = self._card_age_ms(
            card,
            fetch_age_ms=fetch_age,
            payload_age_ms=payload_age,
        )
        return evaluate_forecast_value(
            p_up_external=card.get("p_up_external"),
            confidence=(
                float(card["confidence"])
                if card.get("confidence") is not None
                else None
            ),
            model_version=str(card.get("model_version") or "") or None,
            age_ms=age_ms,
            max_age_ms=self.max_age_ms,
            minimum_p_up=minimum_p_up,
            maximum_p_up=maximum_p_up,
            combo_key=str(combo_key),
            condition_suffix=condition_suffix,
        )


def provider_from_settings(settings: Any) -> P25StateForecastProvider:
    return P25StateForecastProvider(
        state_url=settings.dual40_forecast_state_url,
        timeout_ms=settings.dual40_forecast_timeout_ms,
        max_age_ms=settings.dual40_forecast_max_age_ms,
        cache_ms=settings.dual40_forecast_cache_ms,
        tte_tolerance_sec=settings.dual40_forecast_tte_tolerance_sec,
    )
