"""P1 test seti — ag baglantisi olmadan calisan cekirdek birimler.

Kapsam: combo/horizon izolasyonu, discovery ayrisimi (slug/active-event/resolution
metadata/resmi sonuc), diff-depth local book senkronu, reference routing + candle
open, freshness reddi, recorder + resmi etiket backfill.
"""
from __future__ import annotations

import json

import pytest

from config import Settings
from discovery import (
    build_slug,
    classify_resolution,
    match_combo,
    parse_event_markets,
    parse_resolved_outcome,
    window_start,
)
from models import (
    Asset,
    AssetHorizon,
    Decision,
    FeatureSnapshot,
    Horizon,
    MarketRef,
    Prediction,
    ResolutionType,
    all_combos,
)


# --------------------------------------------------------------------------
# combo / horizon izolasyonu
# --------------------------------------------------------------------------


def test_all_combos_are_12_and_unique():
    combos = all_combos()
    assert len(combos) == 12
    assert len({c.key for c in combos}) == 12


def test_binance_symbol_and_horizon_seconds():
    ah = AssetHorizon(Asset.SOL, Horizon.H15M)
    assert ah.binance_symbol == "SOLUSDT"
    assert ah.key == "SOL:15m"
    assert Horizon.H5M.seconds == 300
    assert Horizon.H1H.seconds == 3600


# --------------------------------------------------------------------------
# discovery: slug / combo eslesme / resolution metadata / resmi sonuc
# --------------------------------------------------------------------------


def test_build_and_match_slug():
    slug = build_slug(Asset.BTC, Horizon.H5M, 1700000000)
    assert slug == "btc-updown-5m-1700000000"
    assert match_combo(slug) == AssetHorizon(Asset.BTC, Horizon.H5M)


def test_match_combo_hourly_alias():
    assert match_combo("eth-updown-1h-1700000000") == AssetHorizon(Asset.ETH, Horizon.H1H)
    assert match_combo("xrp-updown-60m-1700000000") == AssetHorizon(Asset.XRP, Horizon.H1H)
    assert match_combo("random text") is None


def test_window_start_rounds_to_period():
    assert window_start(Horizon.H5M, 1700000123) == 1700000123 - (1700000123 % 300)


def test_classify_resolution_source():
    assert classify_resolution("Resolves per Chainlink BTC/USD", Horizon.H5M) == ResolutionType.CHAINLINK
    assert classify_resolution("uses Binance 1h candle close", Horizon.H1H) == ResolutionType.BINANCE_CANDLE
    assert classify_resolution("UMA optimistic oracle", Horizon.H1H) == ResolutionType.UMA
    # bos/taninmayan -> UNKNOWN (varsayim yok)
    assert classify_resolution("", Horizon.H5M) == ResolutionType.UNKNOWN


def _fake_event(slug="btc-updown-5m-1700000000", source="Resolves according to Chainlink BTC/USD"):
    return {
        "slug": slug,
        "title": "Bitcoin Up or Down?",
        "markets": [
            {
                "conditionId": "0xabc",
                "question": "BTC Up?",
                "clobTokenIds": json.dumps(["111", "222"]),
                "outcomes": json.dumps(["Up", "Down"]),
                "startDate": "2026-08-18T00:00:00Z",
                "endDate": "2026-08-18T00:05:00Z",
                "resolutionSource": source,
                "closed": False,
            }
        ],
    }


def test_parse_event_markets_extracts_mandatory_resolution_meta():
    ref = parse_event_markets(_fake_event())
    assert ref is not None
    assert ref.combo == AssetHorizon(Asset.BTC, Horizon.H5M)
    assert ref.up_token_id == "111"
    assert ref.down_token_id == "222"
    assert ref.resolution_type == ResolutionType.CHAINLINK
    assert ref.has_resolution_meta is True


def test_parse_event_markets_flags_missing_resolution_meta():
    ref = parse_event_markets(_fake_event(source=""))
    assert ref is not None
    assert ref.resolution_type == ResolutionType.UNKNOWN
    assert ref.has_resolution_meta is False


def test_parse_resolved_outcome_official():
    up = {"closed": True, "outcomePrices": json.dumps(["1", "0"]), "outcomes": json.dumps(["Up", "Down"])}
    down = {"closed": True, "outcomePrices": json.dumps(["0", "1"]), "outcomes": json.dumps(["Up", "Down"])}
    assert parse_resolved_outcome(up) == Decision.UP
    assert parse_resolved_outcome(down) == Decision.DOWN
    # kapanmamis / belirsiz -> None
    assert parse_resolved_outcome({"closed": False, "outcomePrices": json.dumps(["1", "0"])}) is None
    assert parse_resolved_outcome({"closed": True, "outcomePrices": json.dumps(["0.5", "0.5"])}) is None


# --------------------------------------------------------------------------
# binance diff-depth local book senkronu (gercek OFI temeli)
# --------------------------------------------------------------------------


def _symbol_feed():
    from binance_feed import SymbolFeed

    return SymbolFeed("BTCUSDT", 100)


def test_local_book_snapshot_sync_and_diff_apply():
    feed = _symbol_feed()
    # senkron oncesi bir diff tamponla (u=7 >= lastUpdateId+1=6, U=6<=6<=7)
    feed.on_depth({"E": 1, "U": 6, "u": 7, "b": [["100", "1"]], "a": [["101", "2"]]})
    feed.apply_snapshot({"lastUpdateId": 5, "bids": [["99", "3"]], "asks": [["102", "4"]]})
    assert feed.book.synced is True
    assert feed.book.best_bid == 100.0  # diff bid seviyesi eklendi
    assert feed.book.best_ask == 101.0
    assert feed.book.mid == pytest.approx(100.5)


def test_local_book_level_removal_on_zero_size():
    feed = _symbol_feed()
    feed.apply_snapshot({"lastUpdateId": 10, "bids": [["99", "3"], ["98", "5"]], "asks": [["101", "2"]]})
    assert feed.book.synced is True
    # 99 seviyesini sil (size 0), sureklilik U=prev_u+1=11
    feed.on_depth({"E": 2, "U": 11, "u": 11, "b": [["99", "0"]], "a": []})
    assert 99.0 not in feed.book.bids
    assert feed.book.best_bid == 98.0


def test_local_book_gap_triggers_resync():
    feed = _symbol_feed()
    feed.apply_snapshot({"lastUpdateId": 10, "bids": [["99", "3"]], "asks": [["101", "2"]]})
    # bosluk: U=20 >> prev_u+1=11 -> yeniden senkron gerek
    feed.on_depth({"E": 3, "U": 20, "u": 21, "b": [["99", "9"]], "a": []})
    assert feed.book.synced is False


# --------------------------------------------------------------------------
# reference: horizon routing + candle open
# --------------------------------------------------------------------------


def test_reference_router_picks_adapter_by_horizon():
    from reference import ReferenceRouter

    r = ReferenceRouter(Settings())
    assert r.adapter_for(Horizon.H5M) is r.chainlink
    assert r.adapter_for(Horizon.H15M) is r.chainlink
    assert r.adapter_for(Horizon.H1H) is r.binance


def test_pick_candle_open():
    from reference.ref_binance import pick_candle_open

    rows = [[1000, "50", "x", "x", "x", "x", 2000]]
    assert pick_candle_open(rows, 1000) == 50.0
    assert pick_candle_open(rows, 1500) == 50.0  # icinde
    assert pick_candle_open(rows, 2500) is None


# --------------------------------------------------------------------------
# quality: stale reddi
# --------------------------------------------------------------------------


def _snap(**kw):
    base = dict(
        combo=AssetHorizon(Asset.BTC, Horizon.H5M),
        ts=1.0,
        seconds_remaining=100.0,
        spot_age_ms=100.0,
        book_age_ms=100.0,
    )
    base.update(kw)
    return FeatureSnapshot(**base)


def test_freshness_ok_and_stale():
    from models import AbstainReason
    from quality import check_freshness

    s = Settings()
    assert check_freshness(_snap(), s).ok is True
    stale = check_freshness(_snap(spot_age_ms=999999.0), s)
    assert stale.ok is False and stale.reason == AbstainReason.STALE_DATA
    missing = check_freshness(_snap(spot_age_ms=None), s)
    assert missing.ok is False and missing.reason == AbstainReason.STALE_DATA


# --------------------------------------------------------------------------
# recorder: resmi etiket backfill
# --------------------------------------------------------------------------


def _market_ref(cid="0xabc", rtype=ResolutionType.CHAINLINK, source="Chainlink"):
    return MarketRef(
        combo=AssetHorizon(Asset.BTC, Horizon.H5M),
        condition_id=cid,
        slug="btc-updown-5m-1700000000",
        question="BTC Up?",
        up_token_id="111",
        down_token_id="222",
        start_ts=1700000000.0,
        end_ts=1700000300.0,
        resolution_source=source,
        resolution_type=rtype,
    )


def test_recorder_records_and_settles_with_official_label(tmp_path):
    from recorder import Recorder

    rec = Recorder(str(tmp_path / "t.sqlite"))
    ref = _market_ref()
    rec.record_market(ref)
    rec.record_snapshot(ref, _snap(seconds_remaining=60.0, spot_price=100.0), 60)
    rec.record_snapshot(ref, _snap(seconds_remaining=30.0, spot_price=101.0), 30)

    st = rec.stats()
    assert st["markets"] == 1 and st["meta_ok_markets"] == 1
    assert st["snapshots"] == 2 and st["labeled_snapshots"] == 0

    # resmi resolve -> etiket backfill
    ref.resolved = True
    ref.resolved_outcome = Decision.UP
    rec.settle(ref)
    st2 = rec.stats()
    assert st2["resolved_markets"] == 1
    assert st2["labeled_snapshots"] == 2  # iki snapshot da UP etiketlendi
    rec.close()


def test_recorder_flags_missing_resolution_meta(tmp_path):
    from recorder import Recorder

    rec = Recorder(str(tmp_path / "t2.sqlite"))
    rec.record_market(_market_ref(rtype=ResolutionType.UNKNOWN, source=""))
    st = rec.stats()
    assert st["markets"] == 1 and st["meta_ok_markets"] == 0
    rec.close()


# --------------------------------------------------------------------------
# models: price_edge analytics
# --------------------------------------------------------------------------


def test_prediction_price_edge():
    p = Prediction(
        combo=AssetHorizon(Asset.BTC, Horizon.H5M),
        ts=1.0,
        p_up=0.6,
        market_implied_up=0.5,
    )
    assert p.price_edge == pytest.approx(0.1)
    p2 = Prediction(combo=AssetHorizon(Asset.BTC, Horizon.H5M), ts=1.0, p_up=0.6)
    assert p2.price_edge is None
