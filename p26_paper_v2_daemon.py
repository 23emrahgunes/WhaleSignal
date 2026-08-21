"""Disabled-by-default SHADOW runtime for RESEARCH_PAPER_V2.

The daemon consumes only frozen artifacts, chronological OOS calibration, public
CLOB books/fees and the isolated P2.6 database.  It can write paper OPEN/SKIPPED
records, but it cannot sign or submit an order.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np

from p26_alpha_profile import load_alpha_profile, resolve_pretrade_ttl
from p26_artifact import LoadedArtifact, load_artifact
from p26_book_store import BookSnapshotStore
from p26_calibration import ConservativeProbability, conservative_probability
from p26_config import P26Settings, get_p26_settings
from p26_fee import FeeScheduleStore
from p26_features import EXTERNAL_FEATURE_NAMES, vector_from_mapping
from p26_latency import SourceClock
from p26_paper_v2 import (
    PaperV2Decision,
    evaluate_ex_post_alpha,
    evaluate_paper_v2_sides,
)
from p26_paper_v2_recorder import PaperV2Recorder
from p26_portfolio_risk import (
    evaluate_portfolio_risk,
    policy_from_settings,
    portfolio_state,
)
from p26_schema import connect_p26, ensure_p26_schema
from p26_selection import RankedCandidate, rank_eligible_candidates


log = logging.getLogger("direction_engine.p26.paper_v2")


def _predict_p_up(artifact: LoadedArtifact, feature_payload: dict) -> float:
    names = artifact.manifest.feature_names_in_exact_order
    if names != list(EXTERNAL_FEATURE_NAMES):
        raise ValueError("MODEL_SCHEMA_MISMATCH")
    X = np.asarray([vector_from_mapping(feature_payload, names)], dtype=float)
    probabilities = artifact.pipeline.predict_proba(X)
    model = getattr(artifact.pipeline, "named_steps", {}).get("model")
    classes = list(getattr(model, "classes_", [0, 1]))
    return float(probabilities[0, classes.index(1)])


def _source_clock(row, clob_ts_ms: Optional[int]) -> SourceClock:  # noqa: ANN001
    return SourceClock(
        int(row["binance_trade_ts_ms"]) if row["binance_trade_ts_ms"] is not None else None,
        int(row["binance_book_ts_ms"]) if row["binance_book_ts_ms"] is not None else None,
        int(row["chainlink_source_ts_ms"]) if row["chainlink_source_ts_ms"] is not None else None,
        int(row["clob_quote_ts_ms"]) if row["clob_quote_ts_ms"] is not None else clob_ts_ms,
    )


def _synthetic_skip(
    *,
    side: str,
    reason: str,
    probability: Optional[ConservativeProbability],
    details: tuple[str, ...] = (),
) -> PaperV2Decision:
    scope = probability.scope if probability is not None else "NONE"
    lower = probability.selected_lower(side) if probability is not None else None
    return PaperV2Decision(
        False, reason, side, None, lower, None, None, None, None, None, None,
        scope, None, details,
    )


class PaperV2Runtime:
    def __init__(self, settings: P26Settings) -> None:
        self.settings = settings
        self.conn = connect_p26(settings.p26_db_path)
        ensure_p26_schema(self.conn)
        self.books = BookSnapshotStore(settings.p26_db_path)
        self.fees = FeeScheduleStore(settings.p26_db_path)
        self.recorder = PaperV2Recorder(settings.p26_db_path)
        self.risk_policy = policy_from_settings(settings)
        self.cursor_key = f"paper_v2_canonical_cursor:{settings.paper_v2_strategy_version}"
        self.model: Optional[LoadedArtifact] = None
        self.alpha_profile = None

    def _meta_int(self, key: str, default: int = 0) -> int:
        row = self.conn.execute("SELECT value FROM p26_meta WHERE key=?", (key,)).fetchone()
        try:
            return int(row["value"]) if row is not None else int(default)
        except (TypeError, ValueError):
            return int(default)

    def _set_meta_int(self, key: str, value: int) -> None:
        self.conn.execute(
            """
            INSERT INTO p26_meta(key,value,updated_at_ms) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at_ms=excluded.updated_at_ms
            """,
            (key, str(int(value)), int(time.time() * 1000)),
        )
        self.conn.commit()

    def load_artifacts(self) -> tuple[bool, str]:
        model_path = Path(self.settings.paper_v2_model_manifest)
        alpha_path = Path(self.settings.paper_v2_alpha_artifact)
        if not model_path.exists():
            return False, "MODEL_ARTIFACT_NOT_READY"
        if not alpha_path.exists():
            return False, "ALPHA_ARTIFACT_NOT_READY"
        try:
            self.model = load_artifact(model_path)
            self.alpha_profile = load_alpha_profile(alpha_path)
        except Exception as exc:  # noqa: BLE001
            log.exception("artifact load failed")
            return False, f"ARTIFACT_LOAD_FAILED:{type(exc).__name__}"
        return True, "READY"

    def candidates(self, now_ms: int, limit: int = 250):
        cursor = self._meta_int(self.cursor_key, 0)
        return self.conn.execute(
            """
            SELECT * FROM p26_canonical_rows
            WHERE id>? AND training_eligible=1
              AND decision_ts_ms+?<=?
            ORDER BY decision_ts_ms,id LIMIT ?
            """,
            (cursor, self.settings.paper_v2_fill_delay_ms, int(now_ms), int(limit)),
        ).fetchall()

    def _risk_state(self, row, now_ms: int):  # noqa: ANN001
        asset = str(row["asset"])
        return portfolio_state(
            self.recorder.conn,
            policy=self.risk_policy,
            asset=asset,
            horizon=str(row["horizon"]),
            strategy_version=self.settings.paper_v2_strategy_version,
            now_ms=now_ms,
        )

    def evaluate_row(self, row):  # noqa: ANN001
        assert self.model is not None and self.alpha_profile is not None
        condition_id = str(row["condition_id"])
        combo_key = str(row["combo_key"])
        horizon = str(row["horizon"])
        forecast_ts = int(row["decision_ts_ms"])
        fill_ts = forecast_ts + self.settings.paper_v2_fill_delay_ms
        feature_payload = json.loads(str(row["feature_vector_json"]))
        p_up_raw = _predict_p_up(self.model, feature_payload)
        probability = conservative_probability(
            self.conn,
            self.settings,
            p_up_raw=p_up_raw,
            combo_key=combo_key,
            horizon=horizon,
            cutoff_ts_ms=forecast_ts,
            model_version=self.model.manifest.artifact_version,
        )
        alpha_ttl = resolve_pretrade_ttl(
            self.alpha_profile,
            combo_key=combo_key,
            horizon=horizon,
            regime=str(row["quality_status"] or "ALL"),
            decision_ts_ms=forecast_ts,
            approved_scopes=self.settings.approved_alpha_scopes(),
        )
        mapping = self.fees.mapping(condition_id)
        if set(mapping) != {"UP", "DOWN"}:
            side = "UP" if p_up_raw >= 0.5 else "DOWN"
            return _synthetic_skip(
                side=side, reason="TOKEN_MAPPING_NOT_READY", probability=probability
            ), probability, fill_ts
        start = forecast_ts - max(2_000, self.settings.paper_v2_min_depth_persistence_ms * 4)
        up_books = self.books.history(condition_id, "UP", start_ts_ms=start, end_ts_ms=fill_ts)
        down_books = self.books.history(condition_id, "DOWN", start_ts_ms=start, end_ts_ms=fill_ts)
        up_latest = up_books[-1].ts_ms if up_books else None
        down_latest = down_books[-1].ts_ms if down_books else None
        state = self._risk_state(row, fill_ts)
        selection, up, down = evaluate_paper_v2_sides(
            self.settings,
            probability=probability,
            forecast_ts_ms=forecast_ts,
            fill_ts_ms=fill_ts,
            up_source_clock=_source_clock(row, up_latest),
            down_source_clock=_source_clock(row, down_latest),
            up_books=up_books,
            down_books=down_books,
            alpha_ttl=alpha_ttl,
            up_fee_schedule=self.fees.get(condition_id, mapping["UP"]),
            down_fee_schedule=self.fees.get(condition_id, mapping["DOWN"]),
            portfolio_state=state,
            portfolio_policy=self.risk_policy,
        )
        if selection.eligible and selection.decision is not None:
            return selection.decision, probability, fill_ts
        preferred = up if p_up_raw >= 0.5 else down
        return replace(
            preferred,
            eligible=False,
            reason=selection.reason,
            details=selection.details,
        ), probability, fill_ts

    def process(self, now_ms: Optional[int] = None) -> dict:
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        ready, reason = self.load_artifacts()
        settled = self.recorder.settle_available_labels()
        if not ready:
            return {"status": "NOT_READY", "reason": reason, "settled": settled, "processed": 0}
        rows = self.candidates(now)
        if not rows:
            self._record_ex_post(now)
            return {"status": "IDLE", "settled": settled, "processed": 0}

        evaluated: list[tuple[object, PaperV2Decision, int]] = []
        for row in rows:
            if self.recorder.attempt_exists(str(row["condition_id"]), self.settings.paper_v2_strategy_version):
                continue
            fill_ts = int(row["decision_ts_ms"]) + self.settings.paper_v2_fill_delay_ms
            label = self.conn.execute(
                "SELECT official_label FROM p26_labels WHERE condition_id=?",
                (row["condition_id"],),
            ).fetchone()
            if int(row["market_end_ts_ms"]) <= fill_ts:
                decision = _synthetic_skip(
                    side="UP", reason="MARKET_LIFECYCLE_INVALID", probability=None
                )
            elif now - fill_ts > self.settings.max_forecast_age_ms:
                decision = _synthetic_skip(
                    side="UP", reason="RUNTIME_MISSED_ENTRY_WINDOW", probability=None,
                    details=(f"runtime_lag_ms={now-fill_ts}",),
                )
            elif label is not None and label["official_label"] is not None:
                decision = _synthetic_skip(
                    side="UP", reason="MARKET_ALREADY_RESOLVED", probability=None
                )
            else:
                try:
                    decision, _, fill_ts = self.evaluate_row(row)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Paper V2 row evaluation failed condition=%s", row["condition_id"])
                    decision = _synthetic_skip(
                        side="UP",
                        reason="RUNTIME_EVALUATION_ERROR",
                        probability=None,
                        details=(repr(exc),),
                    )
            evaluated.append((row, decision, fill_ts))

        eligible = [
            RankedCandidate(
                combo_key=str(row["combo_key"]),
                condition_id=str(row["condition_id"]),
                decision=decision,
                decision_ts_ms=int(row["decision_ts_ms"]),
            )
            for row, decision, _ in evaluated
            if decision.eligible
        ]
        rank = {
            item.condition_id: index + 1
            for index, item in enumerate(rank_eligible_candidates(eligible))
        }
        processed = opened = skipped = 0
        for row, decision, fill_ts in sorted(
            evaluated, key=lambda item: (rank.get(str(item[0]["condition_id"]), 10**9), int(item[0]["id"]))
        ):
            # Re-evaluate state in ranking order so earlier selected candidates
            # consume paper exposure before later correlated candidates.
            if decision.eligible and decision.fill is not None:
                state = self._risk_state(row, fill_ts)
                risk = evaluate_portfolio_risk(
                    state,
                    policy=self.risk_policy,
                    candidate_stake_usdc=self.settings.paper_v2_stake_usdc,
                    projected_fee_usdc=decision.fill.fee_usdc,
                    now_ms=fill_ts,
                )
                if not risk.allowed:
                    decision = replace(
                        decision,
                        eligible=False,
                        reason=risk.reason,
                        portfolio=risk,
                        details=risk.details,
                    )
            if self.recorder.record(
                condition_id=str(row["condition_id"]),
                combo_key=str(row["combo_key"]),
                horizon=str(row["horizon"]),
                forecast_ts_ms=int(row["decision_ts_ms"]),
                fill_ts_ms=fill_ts,
                decision=decision,
                stake_usdc=self.settings.paper_v2_stake_usdc,
                model_artifact_id=self.model.manifest.artifact_id,
                selection_reason=("RANKED" if decision.eligible else decision.reason),
            ):
                processed += 1
                opened += int(decision.eligible)
                skipped += int(not decision.eligible)
        self._set_meta_int(self.cursor_key, max(int(row["id"]) for row in rows))
        self._record_ex_post(now)
        return {
            "status": "OK",
            "processed": processed,
            "opened": opened,
            "skipped": skipped,
            "settled": settled,
        }

    def _record_ex_post(self, now_ms: int) -> int:
        rows = self.recorder.conn.execute(
            """
            SELECT t.* FROM p26_paper_trades t
            LEFT JOIN p26_alpha_replays a
              ON a.condition_id=t.condition_id AND a.strategy_version=t.strategy_version
            WHERE t.status IN ('OPEN','SETTLED') AND a.id IS NULL
              AND t.fill_ts_ms+10200<=?
            ORDER BY t.fill_ts_ms LIMIT 100
            """,
            (int(now_ms),),
        ).fetchall()
        recorded = 0
        for row in rows:
            schedule = self.fees.get(str(row["condition_id"]), str(row["token_id"] or ""))
            if schedule is None or row["selected_probability_lower"] is None:
                continue
            books = self.books.history(
                str(row["condition_id"]), str(row["side"]),
                start_ts_ms=int(row["forecast_ts_ms"]),
                end_ts_ms=int(row["fill_ts_ms"]) + 10_200,
            )
            replay = evaluate_ex_post_alpha(
                forecast_ts_ms=int(row["forecast_ts_ms"]),
                books=books,
                conservative_probability=float(row["selected_probability_lower"]),
                stake_usdc=float(row["stake_usdc"] or self.settings.paper_v2_stake_usdc),
                fee_schedule=schedule,
                safety_buffer=self.settings.paper_v2_safety_buffer,
            )
            recorded += int(
                self.recorder.record_ex_post_alpha(
                    condition_id=str(row["condition_id"]),
                    combo_key=str(row["combo_key"]), horizon=str(row["horizon"]),
                    side=str(row["side"]), forecast_ts_ms=int(row["forecast_ts_ms"]),
                    replay=replay,
                )
            )
        return recorded

    def close(self) -> None:
        self.books.close()
        self.fees.close()
        self.recorder.close()
        self.conn.close()


async def run(interval_sec: float = 5.0) -> None:
    settings = get_p26_settings()
    runtime = PaperV2Runtime(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        while not stop.is_set():
            if not settings.paper_v2_enabled:
                log.info("Paper V2 disabled fail-closed; data collection continues")
            else:
                result = runtime.process()
                log.info("Paper V2 cycle %s", json.dumps(result, sort_keys=True))
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1.0, interval_sec))
            except asyncio.TimeoutError:
                pass
    finally:
        runtime.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
