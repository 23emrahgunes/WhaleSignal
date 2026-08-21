from pathlib import Path

from p26_alpha_profile import (
    AlphaProfileBucket,
    FrozenAlphaProfile,
    load_alpha_profile,
    resolve_pretrade_ttl,
    save_alpha_profile,
)
from p26_delay_replay import replay_edge_curve
from p26_execution import OrderBookSnapshot


def _profile(history_max=900, scope="PER_COMBO"):
    return FrozenAlphaProfile(
        artifact_id="alpha-v1",
        created_at_ms=950,
        code_commit="abc",
        source_model_version="model-v1",
        minimum_samples=3,
        buckets=(
            AlphaProfileBucket(
                scope=scope,
                key="BTC:5m|CHOP" if scope == "PER_COMBO" else "5m|CHOP",
                regime="CHOP",
                ttl_ms=750,
                sample_count=12,
                history_max_ts_ms=history_max,
                quantile=0.2,
            ),
        ),
    )


def test_frozen_alpha_profile_roundtrip_and_past_only(tmp_path):
    path = tmp_path / "alpha.json"
    save_alpha_profile(_profile(), path)
    loaded = load_alpha_profile(path)
    decision = resolve_pretrade_ttl(
        loaded,
        combo_key="BTC:5m",
        horizon="5m",
        regime="CHOP",
        decision_ts_ms=1000,
    )
    assert decision.ready
    assert decision.ttl_ms == 750
    assert decision.scope == "PER_COMBO"


def test_alpha_profile_rejects_future_history_and_unapproved_scope():
    future = resolve_pretrade_ttl(
        _profile(history_max=1000),
        combo_key="BTC:5m",
        horizon="5m",
        regime="CHOP",
        decision_ts_ms=1000,
    )
    assert not future.ready
    assert future.reason == "ALPHA_PROFILE_FUTURE_DATA"

    horizon = resolve_pretrade_ttl(
        _profile(scope="HORIZON"),
        combo_key="BTC:5m",
        horizon="5m",
        regime="CHOP",
        decision_ts_ms=1000,
    )
    assert not horizon.ready
    assert horizon.reason == "ALPHA_SCOPE_NOT_APPROVED"


def test_empty_ex_post_replay_is_fail_closed_not_exception():
    book = OrderBookSnapshot.from_levels(
        token_id="up", ts_ms=10_000,
        bids=[(0.4, 10)], asks=[(0.6, 10)], sequence=1,
    )
    replay = replay_edge_curve(
        forecast_ts_ms=1_000,
        books=[book],
        conservative_probability=0.7,
        stake_usdc=2.5,
        fee_bps=0.0,
        safety_buffer=0.005,
        delays_ms=(0, 100),
        max_book_wait_ms=10,
    )
    assert replay.observations == ()
    assert replay.decay is None
