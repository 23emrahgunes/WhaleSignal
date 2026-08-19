"""P2.2-P2.5 shadow engine layered on the proven P1/P2.1 runtime."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from main import ShadowEngine
from models import (
    AbstainReason,
    Decision,
    Horizon,
    LabelStatus,
    Prediction,
    QStatus,
    Regime,
)
from p25_calibration import CalSample as P25CalSample
from p25_model import (
    MODEL_VERSION,
    ModelOutput as P25ModelOutput,
    ptb_heuristic_probability,
)
from p25_regime import RegimeResult, classify_regime
from quality import assess


def decide_chainlink_close(opening, closing):  # noqa: ANN001
    if opening is None or closing is None:
        return None, None
    try:
        open_value = float(opening)
        close_value = float(closing)
    except (TypeError, ValueError):
        return None, None
    if open_value <= 0 or close_value <= 0:
        return None, None
    decision = Decision.UP if close_value >= open_value else Decision.DOWN
    return decision, "CHAINLINK_DATA_STREAM_RTDS_CLOSE"


@dataclass
class DecisionBundle:
    prediction: Prediction
    trace: dict
    regime: Optional[RegimeResult]
    model_output: Optional[P25ModelOutput]


class P25Engine(ShadowEngine):
    """Predictability -> model -> calibration -> forecast, always SHADOW."""

    def __init__(self, cfg, hub, recorder, model, calib) -> None:  # noqa: ANN001
        super().__init__(cfg, hub, recorder, model, calib)
        self._cal_samples: dict[str, list[P25CalSample]] = {}
        self._forecast_writes = 0

    @staticmethod
    def _data_ready(q) -> bool:  # noqa: ANN001
        return (
            q.snapshot_recordable
            and q.clob == QStatus.OK
            and q.reference == QStatus.OK
            and q.clock == QStatus.OK
        )

    @staticmethod
    def _data_reason(q) -> AbstainReason:  # noqa: ANN001
        if q.time == QStatus.FAIL:
            return AbstainReason.UNSAFE_TIME_METADATA
        if q.market == QStatus.FAIL or q.tokens == QStatus.FAIL:
            return AbstainReason.UNSAFE
        if any(note == "feed:transport_stale" for note in q.notes):
            return AbstainReason.STALE_DATA
        if q.clock == QStatus.FAIL:
            return AbstainReason.CLOCK_UNSYNC
        if q.clob != QStatus.OK:
            return AbstainReason.CLOB_MISSING
        if q.reference != QStatus.OK:
            return AbstainReason.PTB_MISSING
        return AbstainReason.INSUFFICIENT_DATA

    def _empty_trace(self, ref, fv, snap) -> dict:  # noqa: ANN001
        return {
            "phase": self.cfg.phase,
            "model_version": MODEL_VERSION,
            "model_source": "none",
            "feature_ready": bool(fv and fv.feature_ready),
            "feature_coverage": float(
                getattr(fv, "feature_coverage", 0.0) or 0.0
            ),
            "predictability": 0.0,
            "conflict_score": 0.0,
            "directional_consensus": 0.0,
            "regime": Regime.UNKNOWN.value,
            "p_up_raw": None,
            "p_up_calibrated": None,
            "p_up_ptb": None,
            "p_up_ptb_heuristic": (
                ptb_heuristic_probability(fv) if fv is not None else None
            ),
            "p_up_external": None,
            "p_up_market": snap.up_mid,
            "confidence": 0.0,
            "decision": Decision.ABSTAIN.value,
            "abstain_reason": AbstainReason.INSUFFICIENT_DATA.value,
            "threshold": self.cfg.default_probability_threshold(
                ref.combo.horizon.value
            ),
            "threshold_source": "DEFAULT",
            "calibration_source": "OFF",
            "calibration_n": 0,
        }

    def decide(self, ref, snap, q, fv) -> DecisionBundle:  # noqa: ANN001
        trace = self._empty_trace(ref, fv, snap)
        market_up = snap.up_mid

        if not self._data_ready(q):
            reason = self._data_reason(q)
            trace["abstain_reason"] = reason.value
            return DecisionBundle(
                Prediction(
                    combo=ref.combo,
                    ts=snap.ts,
                    decision=Decision.ABSTAIN,
                    abstain_reason=reason,
                    reasons=list(q.notes),
                    market_implied_up=market_up,
                ),
                trace,
                None,
                None,
            )

        if fv is None or not fv.feature_ready:
            missing = list(getattr(fv, "missing_features", []) or [])
            trace["abstain_reason"] = AbstainReason.INSUFFICIENT_DATA.value
            return DecisionBundle(
                Prediction(
                    combo=ref.combo,
                    ts=snap.ts,
                    decision=Decision.ABSTAIN,
                    abstain_reason=AbstainReason.INSUFFICIENT_DATA,
                    reasons=[
                        "feature warmup"
                        + (f": {','.join(missing)}" if missing else "")
                    ],
                    market_implied_up=market_up,
                ),
                trace,
                None,
                None,
            )

        regime = classify_regime(
            fv,
            min_predictability=self.cfg.min_predictability(
                ref.combo.horizon.value
            ),
        )
        trace.update(
            {
                "predictability": regime.predictability,
                "conflict_score": regime.conflict_score,
                "directional_consensus": regime.directional_consensus,
                "regime": regime.regime.value,
            }
        )

        if regime.abstain:
            trace["abstain_reason"] = regime.abstain_reason.value
            return DecisionBundle(
                Prediction(
                    combo=ref.combo,
                    ts=snap.ts,
                    predictability=regime.predictability,
                    regime=regime.regime,
                    decision=Decision.ABSTAIN,
                    abstain_reason=regime.abstain_reason,
                    reasons=regime.reasons,
                    market_implied_up=market_up,
                ),
                trace,
                regime,
                None,
            )

        if not self.cfg.model_inference_active:
            trace["abstain_reason"] = AbstainReason.MODEL_NOT_TRAINED.value
            return DecisionBundle(
                Prediction(
                    combo=ref.combo,
                    ts=snap.ts,
                    predictability=regime.predictability,
                    regime=regime.regime,
                    decision=Decision.ABSTAIN,
                    abstain_reason=AbstainReason.MODEL_NOT_TRAINED,
                    reasons=regime.reasons + ["direction model disabled by phase"],
                    market_implied_up=market_up,
                ),
                trace,
                regime,
                None,
            )

        output = self.model.predict(ref.combo.key, fv)
        trace.update(
            {
                "model_source": output.source,
                "p_up_raw": output.p_up,
                "p_up_external": output.p_up_no_clob,
                "p_up_ptb": output.p_up_ptb,
                "p_up_ptb_heuristic": output.p_up_ptb_heuristic,
            }
        )
        if not output.ready or output.p_up is None:
            trace["abstain_reason"] = AbstainReason.MODEL_NOT_TRAINED.value
            return DecisionBundle(
                Prediction(
                    combo=ref.combo,
                    ts=snap.ts,
                    predictability=regime.predictability,
                    regime=regime.regime,
                    decision=Decision.ABSTAIN,
                    abstain_reason=AbstainReason.MODEL_NOT_TRAINED,
                    reasons=regime.reasons + ["resolved market warmup"],
                    market_implied_up=market_up,
                ),
                trace,
                regime,
                output,
            )

        raw_p = float(output.p_up)
        if self.cfg.calibration_active:
            cal = self.calib.calibrate(
                ref.combo.key,
                raw_p,
                min_samples=self.cfg.calibration_min_samples,
                min_bin_samples=self.cfg.calibration_min_bin_samples,
                prior_strength=self.cfg.calibration_prior_strength,
            )
            p_up = cal.p_up
            calibration_source = cal.source
            calibration_n = cal.n
        else:
            p_up = raw_p
            calibration_source = "OFF_RAW"
            calibration_n = 0

        threshold = self.calib.decision_threshold(
            ref.combo.key,
            default=self.cfg.default_probability_threshold(
                ref.combo.horizon.value
            ),
            min_samples=self.cfg.threshold_min_samples,
            min_covered=self.cfg.threshold_min_covered,
            target_accuracy=self.cfg.threshold_target_accuracy,
        )
        chosen = max(p_up, 1.0 - p_up)
        confidence = min(
            1.0,
            2.0 * abs(p_up - 0.5) * regime.predictability,
        )
        if chosen < threshold.threshold:
            decision = Decision.ABSTAIN
            reason = AbstainReason.LOW_PREDICTABILITY
            reasons = regime.reasons + [
                f"p_chosen={chosen:.3f}<threshold={threshold.threshold:.3f}"
            ]
        else:
            decision = Decision.UP if p_up >= 0.5 else Decision.DOWN
            reason = AbstainReason.NONE
            reasons = self._why_p25(
                fv, regime, output, p_up, threshold.threshold
            )

        trace.update(
            {
                "p_up_calibrated": p_up,
                "calibration_source": calibration_source,
                "calibration_n": calibration_n,
                "threshold": threshold.threshold,
                "threshold_source": threshold.source,
                "confidence": confidence,
                "decision": decision.value,
                "abstain_reason": reason.value,
            }
        )
        prediction = Prediction(
            combo=ref.combo,
            ts=snap.ts,
            p_up=p_up,
            p_down=1.0 - p_up,
            confidence=confidence,
            predictability=regime.predictability,
            regime=regime.regime,
            decision=decision,
            abstain_reason=reason,
            reasons=reasons,
            market_implied_up=market_up,
        )
        return DecisionBundle(prediction, trace, regime, output)

    @staticmethod
    def _why_p25(fv, regime, output, p_up, threshold) -> list[str]:  # noqa: ANN001
        reasons = [
            f"p_up={p_up:.3f} threshold={threshold:.3f}",
            f"regime={regime.regime.value}",
            f"predictability={regime.predictability:.2f}",
            f"PTB={fv.distance_bps:+.1f}bps z={fv.ptb_z:+.2f}",
            f"momentum={fv.ret_slow * 10000:+.2f}bps "
            f"persist={fv.sign_persistence:.2f}",
            f"flow={fv.flow_mid:+.2f} OBI={fv.obi_20:+.2f}",
        ]
        if output.p_up_no_clob is not None:
            reasons.append(f"B1_external={output.p_up_no_clob:.3f}")
        if output.p_up_ptb is not None:
            reasons.append(f"PTB_model={output.p_up_ptb:.3f}")
        return reasons

    def tick(self) -> None:
        active = self.hub.discovery.snapshot_active()
        now = time.time()
        present: set[str] = set()

        for key, ref in active.items():
            present.add(key)
            self._maybe_record_market(ref)
            self._cal_samples.setdefault(ref.market_id, [])
            snap = self.hub.build_snapshot(ref, now)
            model_ready = (
                self.model.ready_for(ref.combo.key)
                if self.cfg.model_inference_active
                else False
            )
            q = assess(
                ref,
                snap,
                self.cfg,
                now,
                self.hub.binance.clock_synced,
                model_ready,
            )
            if q.abstain_reason in (
                AbstainReason.UNSAFE,
                AbstainReason.UNSAFE_TIME_METADATA,
            ):
                self._data_quality_errors += 1

            fv = None
            if q.snapshot_recordable and self.cfg.feature_engine_active:
                feed = self.hub.binance.get_feed(ref.combo.binance_symbol)
                if feed is not None and feed.book is not None:
                    prices = (
                        feed.feature_series()
                        if hasattr(feed, "feature_series")
                        else list(feed.prices)
                    )
                    fv = self._feature_engine(ref).update(
                        prices,
                        list(feed.trades),
                        feed.book,
                        snap.reference_price,
                        snap.up_mid,
                        snap.down_mid,
                        (
                            snap.tte_sec
                            if snap.tte_sec is not None
                            else snap.seconds_remaining
                        ),
                        now,
                        up_bid=snap.up_bid,
                        up_ask=snap.up_ask,
                        down_bid=snap.down_bid,
                        down_ask=snap.down_ask,
                    )

            bundle = self.decide(ref, snap, q, fv)
            prediction = bundle.prediction
            data_ready = self._data_ready(q)
            snap.prediction_ready = (
                data_ready
                and fv is not None
                and fv.feature_ready
                and bundle.regime is not None
                and not bundle.regime.abstain
                and bundle.model_output is not None
                and bundle.model_output.ready
            )
            if not data_ready:
                snap.quality_status = self._data_reason(q).value
            elif fv is None or not fv.feature_ready:
                snap.quality_status = AbstainReason.INSUFFICIENT_DATA.value
            elif bundle.regime is not None and bundle.regime.abstain:
                snap.quality_status = bundle.regime.abstain_reason.value
            elif bundle.model_output is None or not bundle.model_output.ready:
                snap.quality_status = AbstainReason.MODEL_NOT_TRAINED.value
            else:
                snap.quality_status = "OK"

            if q.snapshot_recordable:
                tte = (
                    snap.tte_sec
                    if snap.tte_sec is not None
                    else snap.seconds_remaining
                )
                checkpoint = self._checkpoint_crossed(ref, tte)
                if checkpoint is not None:
                    extra = fv.to_dict() if fv is not None else {}
                    extra["p2_trace"] = bundle.trace
                    if bundle.regime is not None:
                        extra["predictability_components"] = bundle.regime.components
                        extra["regime_reasons"] = bundle.regime.reasons
                    snap.extra = extra
                    self.recorder.record_snapshot(ref, snap, checkpoint)

                    acc = self._acc.get(ref.market_id)
                    if acc is not None and fv is not None and fv.feature_ready:
                        acc.fvs.append(fv)

                    if self.cfg.forecast_recording_active:
                        if self.recorder.record_forecast(
                            ref, snap, checkpoint, bundle.trace
                        ):
                            self._forecast_writes += 1

                    raw_p = bundle.trace.get("p_up_raw")
                    if raw_p is not None:
                        self._cal_samples[ref.market_id].append(
                            P25CalSample(
                                decided=prediction.decision
                                in (Decision.UP, Decision.DOWN),
                                outcome_up=False,
                                p_up=float(raw_p),
                                decision_up=float(raw_p) >= 0.5,
                                confidence=float(
                                    bundle.trace.get("confidence") or 0.0
                                ),
                                market_implied_up=bundle.trace.get("p_up_market"),
                                predictability=float(
                                    bundle.trace.get("predictability") or 0.0
                                ),
                                regime=bundle.trace.get("regime"),
                                model_version=bundle.trace.get("model_version"),
                                checkpoint_sec=checkpoint,
                            )
                        )
                    self._event(
                        "FORECAST",
                        f"{ref.combo.key} t-{checkpoint} "
                        f"{prediction.decision.value} "
                        f"p={bundle.trace.get('p_up_calibrated')}",
                    )

            self.latest[key] = self._card_p25(ref, snap, q, bundle, fv)

        for key in list(self.latest):
            if key not in present and self.latest[key].get("active"):
                self.latest[key]["active"] = False
        self._prune_acc()

    async def on_market_resolved(self, ref) -> None:  # noqa: ANN001
        official = ref.official_result or ref.resolved_outcome
        if official is None:
            return

        ref.computed_result, ref.computed_result_source = await self._compute_result(ref)
        ref.computed_result_time = time.time()
        self.recorder.settle(ref)
        self._resolve_count += 1

        # Calibration sees forecasts before the market can update model weights.
        if self.cfg.calibration_active:
            for sample in self._cal_samples.get(ref.market_id, []):
                sample.outcome_up = official == Decision.UP
                self.calib.record(ref.combo.key, sample)
                self._calibration_writes += 1
            self.calib.save(self.cfg.calibration_path)

        trained = False
        acc = self._acc.get(ref.market_id)
        if (
            self.cfg.training_active
            and ref.label_status == LabelStatus.MATCH
            and acc is not None
        ):
            rows = [
                fv for fv in acc.fvs
                if getattr(fv, "feature_ready", False)
            ]
            if rows:
                self.model.learn_with_label(
                    ref.combo.key,
                    rows,
                    1 if official == Decision.UP else 0,
                )
                self._model_learn_calls += 1
                self.model.save(self.cfg.model_path)
                self._model_save_calls += 1
                trained = True

        self._event(
            "RESOLVED",
            f"{ref.combo.key} official={official.value} "
            f"label={ref.label_status.value} trained={trained}",
        )
        self._acc.pop(ref.market_id, None)
        self._cal_samples.pop(ref.market_id, None)

    async def _compute_result(self, ref):  # noqa: ANN001
        if ref.combo.horizon == Horizon.H1H:
            return await self._compute_1h_finalized_candle(ref)

        chainlink = (
            getattr(getattr(self.hub, "reference", None), "chainlink", None)
            if self.hub is not None
            else None
        )
        if (
            chainlink is None
            or ref.market_end_ts is None
            or ref.official_reference_open is None
            or not hasattr(chainlink, "opening_state")
        ):
            return None, None
        closing = chainlink.opening_state(
            ref.combo.asset.value,
            ref.market_end_ts,
            max_alignment_ms=self.cfg.max_reference_close_alignment_ms,
        )
        if closing is None:
            return None, None
        return decide_chainlink_close(
            ref.official_reference_open,
            closing.value,
        )

    def _card_p25(self, ref, snap, q, bundle, fv) -> dict:  # noqa: ANN001
        pred = bundle.prediction

        def rounded(value, digits=3):
            return round(value, digits) if value is not None else None

        feature_view = (
            fv.dashboard()
            if fv is not None
            else {
                "ready": False,
                "coverage": 0.0,
                "history_sec": 0.0,
                "missing": ["feature_engine"],
            }
        )
        return {
            "combo": ref.combo.key,
            "active": True,
            "market_id": (ref.market_id or "")[-8:],
            "slug": ref.slug,
            "condition_id": (ref.condition_id or "")[-8:],
            "up_token": (ref.up_token_id or "")[-8:],
            "down_token": (ref.down_token_id or "")[-8:],
            "tte_sec": rounded(snap.tte_sec, 1),
            "time_status": ref.time_status.value,
            "resolution_type": ref.resolution_type.value,
            "resolution_symbol": ref.resolution_symbol,
            "official_reference_open": rounded(
                snap.official_reference_open, 4
            ),
            "official_reference_source": snap.official_reference_source,
            "proxy_reference_open": rounded(snap.proxy_reference_open, 4),
            "reference_current": rounded(snap.reference_current, 4),
            "reference_current_age_ms": rounded(
                snap.reference_current_age_ms, 0
            ),
            "spot_price": rounded(snap.spot_price, 4),
            "distance_bps": rounded(snap.official_distance_bps, 2),
            "up_bid": rounded(snap.up_bid),
            "up_ask": rounded(snap.up_ask),
            "up_mid": rounded(snap.up_mid),
            "down_bid": rounded(snap.down_bid),
            "down_ask": rounded(snap.down_ask),
            "down_mid": rounded(snap.down_mid),
            "clob_age_ms": rounded(snap.clob_age_ms, 0),
            "transport_age_ms": rounded(snap.transport_age_ms, 0),
            "source_age_ms": rounded(snap.source_age_ms, 0),
            "book_age_ms": rounded(snap.book_age_ms, 0),
            "quality": q.dims(),
            "prediction_ready": snap.prediction_ready,
            "quality_notes": q.notes,
            "feature": feature_view,
            "predictability": rounded(pred.predictability, 3),
            "conflict_score": rounded(
                bundle.trace.get("conflict_score"), 3
            ),
            "directional_consensus": rounded(
                bundle.trace.get("directional_consensus"), 3
            ),
            "regime": pred.regime.value,
            "p_up": rounded(pred.p_up, 4),
            "p_up_raw": rounded(bundle.trace.get("p_up_raw"), 4),
            "p_up_external": rounded(
                bundle.trace.get("p_up_external"), 4
            ),
            "p_up_ptb": rounded(bundle.trace.get("p_up_ptb"), 4),
            "p_up_ptb_heuristic": rounded(
                bundle.trace.get("p_up_ptb_heuristic"), 4
            ),
            "p_up_market": rounded(bundle.trace.get("p_up_market"), 4),
            "confidence": rounded(pred.confidence, 3),
            "threshold": rounded(bundle.trace.get("threshold"), 3),
            "threshold_source": bundle.trace.get("threshold_source"),
            "calibration_source": bundle.trace.get("calibration_source"),
            "model_source": bundle.trace.get("model_source"),
            "model_version": bundle.trace.get("model_version"),
            "decision": pred.decision.value,
            "abstain_reason": pred.abstain_reason.value,
            "why": pred.reasons,
        }

    def snapshot(self) -> dict:
        data = super().snapshot()
        data["phase"] = self.cfg.phase
        data["mode"] = "SHADOW"
        data["forecast_analytics"] = self.recorder.forecast_analytics(
            self.cfg.min_markets_for_stats
        )
        cards = data.get("cards", [])
        data["footer"]["features_ready"] = sum(
            1
            for card in cards
            if card.get("active")
            and card.get("feature", {}).get("ready")
        )
        data["footer"]["model_ready_cards"] = sum(
            1
            for card in cards
            if card.get("active") and card.get("p_up_raw") is not None
        )
        data["footer"]["forecast_writes_runtime"] = self._forecast_writes
        stats = self.recorder.stats()
        data["footer"]["forecasts"] = stats.get("forecasts", 0)
        data["footer"]["labeled_forecasts"] = stats.get(
            "labeled_forecasts", 0
        )
        data["safety"].update(
            {
                "model_inference_enabled": self.cfg.model_inference_active,
                "forecast_recording_enabled": self.cfg.forecast_recording_active,
                "execution_enabled": False,
                "private_key_loaded": False,
            }
        )
        return data
