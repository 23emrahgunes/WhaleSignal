"""Strict paper-only promotion/rejection evaluation for P2.6.

Promotion never enables live trading.  The highest possible state is
``VALIDATED_PAPER_MODEL`` and still has execution/private-key/signing disabled.
"""
from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Iterable, Optional, Sequence

import numpy as np

from p26_config import P26Settings
from p26_eval import ensure_eval_schema
from p26_paper_v2_recorder import ensure_paper_v2_schema
from p26_schema import connect_p26


PROMOTION_STATES = {
    "NOT_READY",
    "REJECTED",
    "RESEARCH_CANDIDATE",
    "VALIDATED_PAPER_MODEL",
}


@dataclass(frozen=True)
class IntervalEstimate:
    point: Optional[float]
    lower: Optional[float]
    upper: Optional[float]
    samples: int


@dataclass(frozen=True)
class PredictiveEvidence:
    n: int
    up_n: int
    down_n: int
    model_brier: Optional[float]
    model_log_loss: Optional[float]
    market_brier: Optional[float]
    market_log_loss: Optional[float]
    naive_brier: Optional[float]
    paired_brier_delta: IntervalEstimate
    paired_log_loss_delta: IntervalEstimate
    positive_fold_fraction: Optional[float]
    asset_concentration: Optional[float]
    horizon_concentration: Optional[float]


@dataclass(frozen=True)
class PaperEvidence:
    n: int
    wins: int
    losses: int
    hit_rate: Optional[float]
    total_stake_usdc: float
    total_pnl_usdc: float
    roi: Optional[float]
    pnl_interval: IntervalEstimate
    max_drawdown_usdc: float
    max_drawdown_fraction: Optional[float]


@dataclass(frozen=True)
class PromotionDecision:
    state: str
    promoted: bool
    reasons: tuple[str, ...]
    predictive: PredictiveEvidence
    paper: PaperEvidence
    policy: dict
    safety: dict

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "promoted": self.promoted,
            "reasons": list(self.reasons),
            "predictive": {
                **asdict(self.predictive),
                "paired_brier_delta": asdict(self.predictive.paired_brier_delta),
                "paired_log_loss_delta": asdict(self.predictive.paired_log_loss_delta),
            },
            "paper": {
                **asdict(self.paper),
                "pnl_interval": asdict(self.paper.pnl_interval),
            },
            "policy": self.policy,
            "safety": self.safety,
        }


def _clip_probability(value: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, float(value)))


def _brier(p: float, y: int) -> float:
    return (_clip_probability(p) - int(y)) ** 2


def _log_loss(p: float, y: int) -> float:
    p = _clip_probability(p)
    return -(int(y) * math.log(p) + (1 - int(y)) * math.log(1.0 - p))


def _mean(values: Sequence[float]) -> Optional[float]:
    return float(np.mean(values)) if values else None


def temporal_block_bootstrap(
    values: Sequence[tuple[int, float]],
    *,
    block_ms: int,
    samples: int,
    random_seed: int,
    statistic: str = "sum",
) -> IntervalEstimate:
    """Bootstrap complete temporal blocks, never individual correlated rows."""
    if not values:
        return IntervalEstimate(None, None, None, 0)
    blocks: dict[int, list[float]] = defaultdict(list)
    for ts_ms, value in values:
        blocks[int(ts_ms) // int(block_ms)].append(float(value))
    ordered_blocks = [group for _, group in sorted(blocks.items())]
    flat = [value for group in ordered_blocks for value in group]
    point = float(np.sum(flat)) if statistic == "sum" else float(np.mean(flat))
    if len(ordered_blocks) == 1 or samples <= 0:
        return IntervalEstimate(point, point, point, len(ordered_blocks))
    rng = np.random.default_rng(int(random_seed))
    distribution: list[float] = []
    for _ in range(int(samples)):
        chosen_indices = rng.integers(0, len(ordered_blocks), size=len(ordered_blocks))
        chosen = [value for index in chosen_indices for value in ordered_blocks[int(index)]]
        distribution.append(float(np.sum(chosen) if statistic == "sum" else np.mean(chosen)))
    lower, upper = np.quantile(np.asarray(distribution), [0.025, 0.975])
    return IntervalEstimate(point, float(lower), float(upper), len(ordered_blocks))


def _maximum_drawdown(pnls: Sequence[float], total_stake: float) -> tuple[float, Optional[float]]:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity += float(pnl)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    fraction = max_drawdown / float(total_stake) if total_stake > 0 else 0.0
    return max_drawdown, fraction


def _load_prediction_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    ensure_eval_schema(conn)
    return conn.execute(
        """
        SELECT condition_id,fold_id,decision_ts_ms,combo_key,horizon,p_up_raw,
               official_label,market_p_up
        FROM p26_oos_predictions
        WHERE role='OUTER_TEST'
        ORDER BY decision_ts_ms,condition_id
        """
    ).fetchall()


def _predictive_evidence(
    rows: Sequence[sqlite3.Row],
    settings: P26Settings,
) -> PredictiveEvidence:
    n = len(rows)
    labels = [int(row["official_label"]) for row in rows]
    model_brier_rows = [_brier(float(row["p_up_raw"]), y) for row, y in zip(rows, labels)]
    model_log_rows = [_log_loss(float(row["p_up_raw"]), y) for row, y in zip(rows, labels)]
    naive_rows = [_brier(0.5, y) for y in labels]

    paired_brier: list[tuple[int, float]] = []
    paired_log: list[tuple[int, float]] = []
    market_brier_rows: list[float] = []
    market_log_rows: list[float] = []
    fold_deltas: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row, y, model_b, model_l in zip(rows, labels, model_brier_rows, model_log_rows):
        market = row["market_p_up"]
        if market is None:
            continue
        market_b = _brier(float(market), y)
        market_l = _log_loss(float(market), y)
        market_brier_rows.append(market_b)
        market_log_rows.append(market_l)
        ts = int(row["decision_ts_ms"])
        paired_brier.append((ts, model_b - market_b))
        paired_log.append((ts, model_l - market_l))
        fold_deltas[str(row["fold_id"])].append((model_b - market_b, model_l - market_l))

    block_ms = settings.promotion_block_hours * 3_600_000
    brier_interval = temporal_block_bootstrap(
        paired_brier,
        block_ms=block_ms,
        samples=settings.promotion_bootstrap_blocks,
        random_seed=settings.promotion_random_seed,
        statistic="mean",
    )
    log_interval = temporal_block_bootstrap(
        paired_log,
        block_ms=block_ms,
        samples=settings.promotion_bootstrap_blocks,
        random_seed=settings.promotion_random_seed + 1,
        statistic="mean",
    )
    positive_folds = 0
    for values in fold_deltas.values():
        if np.mean([value[0] for value in values]) < 0 and np.mean([value[1] for value in values]) < 0:
            positive_folds += 1
    positive_fraction = positive_folds / len(fold_deltas) if fold_deltas else None

    assets = Counter(str(row["combo_key"]).split(":", 1)[0] for row in rows)
    horizons = Counter(str(row["horizon"]) for row in rows)
    asset_concentration = max(assets.values()) / n if n else None
    horizon_concentration = max(horizons.values()) / n if n else None
    return PredictiveEvidence(
        n=n,
        up_n=sum(labels),
        down_n=n - sum(labels),
        model_brier=_mean(model_brier_rows),
        model_log_loss=_mean(model_log_rows),
        market_brier=_mean(market_brier_rows),
        market_log_loss=_mean(market_log_rows),
        naive_brier=_mean(naive_rows),
        paired_brier_delta=brier_interval,
        paired_log_loss_delta=log_interval,
        positive_fold_fraction=positive_fraction,
        asset_concentration=asset_concentration,
        horizon_concentration=horizon_concentration,
    )


def _paper_evidence(conn: sqlite3.Connection, settings: P26Settings) -> PaperEvidence:
    ensure_paper_v2_schema(conn)
    rows = conn.execute(
        """
        SELECT forecast_ts_ms,correct,stake_usdc,realized_pnl
        FROM p26_paper_trades
        WHERE strategy_version=? AND status='SETTLED'
        ORDER BY forecast_ts_ms,id
        """,
        (settings.paper_v2_strategy_version,),
    ).fetchall()
    pnls = [float(row["realized_pnl"] or 0.0) for row in rows]
    stakes = [float(row["stake_usdc"] or 0.0) for row in rows]
    wins = sum(int(row["correct"] or 0) for row in rows)
    interval = temporal_block_bootstrap(
        [(int(row["forecast_ts_ms"]), float(row["realized_pnl"] or 0.0)) for row in rows],
        block_ms=settings.promotion_block_hours * 3_600_000,
        samples=settings.promotion_bootstrap_blocks,
        random_seed=settings.promotion_random_seed + 2,
        statistic="sum",
    )
    total_stake = sum(stakes)
    drawdown, drawdown_fraction = _maximum_drawdown(pnls, total_stake)
    total_pnl = sum(pnls)
    return PaperEvidence(
        n=len(rows),
        wins=wins,
        losses=len(rows) - wins,
        hit_rate=(wins / len(rows) if rows else None),
        total_stake_usdc=total_stake,
        total_pnl_usdc=total_pnl,
        roi=(total_pnl / total_stake if total_stake > 0 else None),
        pnl_interval=interval,
        max_drawdown_usdc=drawdown,
        max_drawdown_fraction=drawdown_fraction,
    )


def evaluate_promotion(settings: P26Settings) -> PromotionDecision:
    conn = connect_p26(settings.p26_db_path)
    try:
        predictive = _predictive_evidence(_load_prediction_rows(conn), settings)
        paper = _paper_evidence(conn, settings)
    finally:
        conn.close()

    reasons: list[str] = []
    not_ready = False
    for ok, reason in (
        (predictive.n >= settings.promotion_min_oos_markets, "OOS_MARKETS_INSUFFICIENT"),
        (predictive.up_n >= settings.promotion_min_oos_class_markets, "OOS_UP_CLASS_INSUFFICIENT"),
        (predictive.down_n >= settings.promotion_min_oos_class_markets, "OOS_DOWN_CLASS_INSUFFICIENT"),
        (paper.n >= settings.promotion_min_paper_trades, "PAPER_V2_TRADES_INSUFFICIENT"),
    ):
        if not ok:
            reasons.append(reason)
            not_ready = True

    predictive_checks = [
        (predictive.model_brier is not None and predictive.model_brier < 0.25, "MODEL_DOES_NOT_BEAT_NAIVE_BRIER"),
        (predictive.market_brier is not None and predictive.model_brier is not None and predictive.model_brier < predictive.market_brier, "MODEL_DOES_NOT_BEAT_MARKET_BRIER"),
        (predictive.market_log_loss is not None and predictive.model_log_loss is not None and predictive.model_log_loss < predictive.market_log_loss, "MODEL_DOES_NOT_BEAT_MARKET_LOGLOSS"),
        (predictive.paired_brier_delta.upper is not None and predictive.paired_brier_delta.upper < 0, "PAIRED_BRIER_CI_NOT_NEGATIVE"),
        (predictive.paired_log_loss_delta.upper is not None and predictive.paired_log_loss_delta.upper < 0, "PAIRED_LOGLOSS_CI_NOT_NEGATIVE"),
        (predictive.positive_fold_fraction is not None and predictive.positive_fold_fraction >= settings.promotion_min_positive_fold_fraction, "FOLD_STABILITY_INSUFFICIENT"),
        (predictive.asset_concentration is not None and predictive.asset_concentration <= settings.promotion_max_asset_concentration, "ASSET_CONCENTRATION_TOO_HIGH"),
        (predictive.horizon_concentration is not None and predictive.horizon_concentration <= settings.promotion_max_horizon_concentration, "HORIZON_CONCENTRATION_TOO_HIGH"),
    ]
    paper_checks = [
        (paper.total_pnl_usdc > 0, "NET_PNL_NOT_POSITIVE"),
        (paper.pnl_interval.lower is not None and paper.pnl_interval.lower > 0, "PNL_BOOTSTRAP_LOWER_NOT_POSITIVE"),
        (paper.max_drawdown_fraction is not None and paper.max_drawdown_fraction <= settings.promotion_max_drawdown_fraction, "DRAWDOWN_TOO_LARGE"),
    ]
    failed = [reason for ok, reason in [*predictive_checks, *paper_checks] if not ok]

    if not_ready:
        # Do not mislabel unavailable performance metrics as rejection evidence.
        # They remain visible as nulls in the report and are evaluated only after
        # the pre-registered sample requirements are met.
        state = "NOT_READY"
    elif failed:
        reasons.extend(failed)
        state = "REJECTED"
    else:
        state = "VALIDATED_PAPER_MODEL"
    assert state in PROMOTION_STATES
    return PromotionDecision(
        state=state,
        promoted=state == "VALIDATED_PAPER_MODEL",
        reasons=tuple(reasons),
        predictive=predictive,
        paper=paper,
        policy={
            "pre_registered_initial_policy": True,
            "min_oos_markets": settings.promotion_min_oos_markets,
            "min_oos_class_markets": settings.promotion_min_oos_class_markets,
            "min_paper_trades": settings.promotion_min_paper_trades,
            "min_positive_fold_fraction": settings.promotion_min_positive_fold_fraction,
            "max_asset_concentration": settings.promotion_max_asset_concentration,
            "max_horizon_concentration": settings.promotion_max_horizon_concentration,
            "max_drawdown_fraction": settings.promotion_max_drawdown_fraction,
            "bootstrap_blocks": settings.promotion_bootstrap_blocks,
            "block_hours": settings.promotion_block_hours,
        },
        safety={
            "mode": "SHADOW_PAPER_ONLY",
            "execution_enabled": False,
            "private_key_loaded": False,
            "signing_enabled": False,
            "order_submission_enabled": False,
            "promotion_ceiling": "VALIDATED_PAPER_MODEL",
        },
    )
