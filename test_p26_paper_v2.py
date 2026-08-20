from __future__ import annotations

from dataclasses import replace

import pytest

from p26_calibration import ConservativeProbability
from p26_config import P26Settings
from p26_execution import OrderBookSnapshot, simulate_buy
from p26_latency import SourceClock
from p26_liquidity_guard import evaluate_liquidity_gate
from p26_paper_v2 import evaluate_paper_v2_entry
from p26_paper_v2_recorder import PaperV2Recorder


def _settings(tmp_path, **updates):
    base = P26Settings(
        p25_db_path=str(tmp_path/"p25.sqlite"),
        p26_db_path=str(tmp_path/"p26.sqlite"),
        paper_v2_stake_usdc=2.5,
        paper_v2_fee_bps=10,
        paper_v2_min_net_edge=0.02,
        paper_v2_safety_buffer=0.005,
        paper_v2_min_depth_persistence_ms=200,
        max_source_skew_ms=100,
        max_decision_data_lag_ms=100,
        max_forecast_age_ms=1000,
        max_quote_age_at_fill_ms=100,
    )
    return base.model_copy(update=updates)


def _prob(up_lower=0.75, down_lower=0.15):
    return ConservativeProbability(
        p_up_raw=0.8,p_up_calibrated=0.78,p_lower_up=up_lower,
        p_lower_down=down_lower,p_upper_up=1-down_lower,bucket_low=0.7,
        bucket_high=0.8,bucket_wins=30,bucket_n=40,scope="OVERALL",
        source="OVERALL_WILSON",calibrator_source="PLATT_PAST_OOS",
        history_max_ts_ms=900,cutoff_ts_ms=1000,
    )


def _books(start=1000, asks=((0.60,10.0),), sequences=(1,2,3,4)):
    return [
        OrderBookSnapshot.from_levels(
            token_id="up",ts_ms=start+i*100,bids=[(0.57,10)],asks=asks,sequence=seq
        ) for i,seq in enumerate(sequences)
    ]


def _clock():
    return SourceClock(980,990,970,995)


def test_depth_vwap_consumes_multiple_levels_and_fee_is_same_unit():
    book=OrderBookSnapshot.from_levels(token_id="u",ts_ms=1,bids=[(0.5,10)],asks=[(0.55,2),(0.60,10)])
    fill=simulate_buy(book,stake_usdc=2.5,fee_bps=100)
    assert fill.complete
    assert fill.levels_consumed==2
    assert fill.orderbook_vwap>0.55
    assert fill.all_in_cost_per_share>fill.orderbook_vwap
    assert fill.price_impact>0


def test_ghost_liquidity_is_vetoed():
    history=[OrderBookSnapshot.from_levels(token_id="u",ts_ms=1000,bids=[(0.5,10)],asks=[(0.6,10)],sequence=1)]
    gate=evaluate_liquidity_gate(history,now_ms=1000,stake_usdc=2.5,max_book_age_ms=100,max_spread=0.2,min_depth_persistence_ms=200,min_fill_fraction=1.0)
    assert not gate.allowed
    assert gate.reason=="GHOST_LIQUIDITY_RISK"


def test_sequence_gap_and_stale_book_are_vetoes():
    gate=evaluate_liquidity_gate(_books(sequences=(1,3)),now_ms=1100,stake_usdc=2.5,max_book_age_ms=100,max_spread=0.2,min_depth_persistence_ms=0,min_fill_fraction=1.0)
    assert gate.reason=="BOOK_SEQUENCE_GAP"
    stale=evaluate_liquidity_gate(_books(),now_ms=2000,stake_usdc=2.5,max_book_age_ms=100,max_spread=0.2,min_depth_persistence_ms=0,min_fill_fraction=1.0)
    assert stale.reason=="STALE_BOOK"


def test_paper_v2_passes_all_gates_with_positive_persistent_edge(tmp_path):
    settings=_settings(tmp_path)
    books=_books(start=1000)
    # Add delayed snapshots required for alpha replay.
    books += [OrderBookSnapshot.from_levels(token_id="up",ts_ms=t,bids=[(0.57,10)],asks=[(0.60,10)],sequence=5+i) for i,t in enumerate((1500,2000))]
    decision=evaluate_paper_v2_entry(
        settings,side="UP",probability=_prob(),forecast_ts_ms=1000,fill_ts_ms=1300,
        source_clock=_clock(),book_history=books,
    )
    assert decision.eligible, decision.to_dict()
    assert decision.reason=="OPEN"
    assert decision.net_edge is not None and decision.net_edge>=settings.paper_v2_min_net_edge


def test_latency_mismatch_vetoes_before_execution(tmp_path):
    settings=_settings(tmp_path)
    decision=evaluate_paper_v2_entry(
        settings,side="UP",probability=_prob(),forecast_ts_ms=1000,fill_ts_ms=1050,
        source_clock=SourceClock(100,990,970,995),book_history=_books(),
    )
    assert not decision.eligible
    assert decision.reason=="LATENCY_MISMATCH"


def test_alpha_decay_vetoes_repriced_edge(tmp_path):
    settings=_settings(tmp_path,paper_v2_min_depth_persistence_ms=0,paper_v2_max_flicker_rate=1.0,paper_v2_max_cancel_to_add_ratio=999.0)
    books=[
        OrderBookSnapshot.from_levels(token_id="u",ts_ms=1000,bids=[(0.57,10)],asks=[(0.60,10)],sequence=1),
        OrderBookSnapshot.from_levels(token_id="u",ts_ms=1100,bids=[(0.67,10)],asks=[(0.70,10)],sequence=2),
        OrderBookSnapshot.from_levels(token_id="u",ts_ms=1250,bids=[(0.73,10)],asks=[(0.76,10)],sequence=3),
    ]
    decision=evaluate_paper_v2_entry(settings,side="UP",probability=_prob(),forecast_ts_ms=1000,fill_ts_ms=1250,source_clock=_clock(),book_history=books)
    assert not decision.eligible
    assert decision.reason in {"ALPHA_EXPIRED","EDGE_DECAYED_BELOW_ZERO","EDGE_DECAYED_BELOW_THRESHOLD","NET_EDGE_BELOW_MINIMUM"}


def test_recorder_isolated_and_idempotent(tmp_path):
    settings=_settings(tmp_path)
    recorder=PaperV2Recorder(settings.p26_db_path)
    books=_books(start=1000)+[OrderBookSnapshot.from_levels(token_id="up",ts_ms=t,bids=[(0.57,10)],asks=[(0.60,10)],sequence=5+i) for i,t in enumerate((1500,2000))]
    decision=evaluate_paper_v2_entry(settings,side="UP",probability=_prob(),forecast_ts_ms=1000,fill_ts_ms=1300,source_clock=_clock(),book_history=books)
    assert recorder.record(condition_id="c1",combo_key="BTC:5m",horizon="5m",forecast_ts_ms=1000,fill_ts_ms=1300,decision=decision,stake_usdc=settings.paper_v2_stake_usdc)
    assert not recorder.record(condition_id="c1",combo_key="BTC:5m",horizon="5m",forecast_ts_ms=1000,fill_ts_ms=1300,decision=decision,stake_usdc=settings.paper_v2_stake_usdc)
    assert recorder.settle("c1","UP",settled_at_ms=2000)==1
    row=recorder.conn.execute("SELECT status,correct,realized_pnl FROM p26_paper_trades WHERE condition_id='c1'").fetchone()
    assert row["status"]=="SETTLED" and row["correct"]==1 and row["realized_pnl"]>0
    recorder.close()


def test_book_snapshot_store_roundtrip_and_dedup(tmp_path):
    from p26_book_store import BookSnapshotStore
    settings=_settings(tmp_path)
    store=BookSnapshotStore(settings.p26_db_path)
    book=OrderBookSnapshot.from_levels(token_id="token",ts_ms=1234,bids=[(0.5,3)],asks=[(0.6,4)],sequence=7)
    assert store.insert(condition_id="c",combo_key="BTC:5m",side="UP",snapshot=book)
    assert not store.insert(condition_id="c",combo_key="BTC:5m",side="UP",snapshot=book)
    rows=store.history("c","UP",start_ts_ms=1000,end_ts_ms=2000)
    assert rows==[book]
    store.close()
