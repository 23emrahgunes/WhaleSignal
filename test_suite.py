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


# ==========================================================================
# P2: features
# ==========================================================================

_AH = AssetHorizon(Asset.BTC, Horizon.H5M)


def test_pct_return_and_realized_vol():
    from features import pct_return, realized_vol

    now_ms = 1_000_000
    prices = [(now_ms - 60000 + i * 1000, 100.0 + i * 0.1) for i in range(61)]
    r = pct_return(prices, 60000, now_ms)
    assert r is not None and r > 0  # yukselen -> pozitif getiri
    rv = realized_vol(prices, 60000, now_ms)
    assert rv is not None and rv >= 0


def test_flow_imbalance_and_obi():
    from features import flow_imbalance, order_book_imbalance
    from models import LocalBook, Trade

    now_ms = 1_000_000
    trades = [Trade(100.0, 1.0, now_ms - 500 * i, is_buyer_maker=False) for i in range(5)]
    fi = flow_imbalance(trades, 5000, now_ms)
    assert fi == pytest.approx(1.0)  # hepsi agresif alis

    book = LocalBook("BTCUSDT", bids={99.9: 10.0, 99.8: 10.0}, asks={100.1: 2.0}, synced=True)
    obi = order_book_imbalance(book)
    assert obi is not None and obi > 0


def test_feature_engine_produces_signed_features():
    from features import FeatureEngine
    from models import LocalBook, Trade

    fe = FeatureEngine(_AH)
    now = 1000.0
    now_ms = int(now * 1000)
    prices = [(now_ms - 60000 + i * 1000, 100.0 + i * 0.02) for i in range(61)]
    trades = [Trade(100.0, 1.0, now_ms - 200 * i, is_buyer_maker=False) for i in range(20)]
    book = LocalBook("BTCUSDT", bids={99.9: 10.0}, asks={100.1: 2.0}, synced=True)
    fv = fe.update(prices, trades, book, 100.0, 0.55, 0.45, 60.0, now)
    assert fv.has_reference is True and fv.distance_bps < 0 or fv.distance_bps >= 0  # hesaplandi
    assert fv.ret_slow > 0
    assert fv.sign_persistence > 0.5
    assert fv.flow_mid > 0
    assert fv.obi > 0
    assert fv.has_clob is True
    # ablation: CLOB'lu varyant 4 fazla feature
    n_base = len(fv.model_features(False)[1])
    n_clob = len(fv.model_features(True)[1])
    assert n_clob == n_base + 4


# ==========================================================================
# P2: regime
# ==========================================================================


def _fv(**kw):
    from features import FeatureVector

    base = dict(combo=_AH, ts=0.0, seconds_remaining=60.0)
    base.update(kw)
    return FeatureVector(**base)


def test_regime_trend_not_abstain():
    from models import Regime
    from regime import classify_regime

    fv = _fv(
        ret_slow=0.001, sign_persistence=0.8, flip_rate=0.1, flow_persistence=0.8,
        flow_mid=0.4, distance_bps=3.0, rv_fast=0.001, rv_slow=0.001,
        vol_percentile=0.5, book_flow_agree=1.0,
    )
    r = classify_regime(fv)
    assert r.abstain is False
    assert r.regime == Regime.TREND_UP
    assert r.predictability >= 0.45


def test_regime_high_vol_abstains():
    from models import AbstainReason
    from regime import classify_regime

    fv = _fv(ret_slow=0.001, rv_fast=0.01, rv_slow=0.005, vol_percentile=0.98)
    r = classify_regime(fv)
    assert r.abstain is True and r.abstain_reason == AbstainReason.HIGH_VOL


def test_regime_feature_conflict_abstains():
    from models import AbstainReason
    from regime import classify_regime

    # momentum + ve PTB + ama flow gucluce - -> conflict
    fv = _fv(
        ret_slow=0.001, distance_bps=5.0, flow_mid=-0.5,
        rv_fast=0.001, rv_slow=0.001, vol_percentile=0.5,
    )
    r = classify_regime(fv)
    assert r.abstain is True and r.abstain_reason == AbstainReason.FEATURE_CONFLICT


def test_regime_insufficient_data():
    from models import AbstainReason
    from regime import classify_regime

    r = classify_regime(_fv())
    assert r.abstain is True and r.abstain_reason == AbstainReason.INSUFFICIENT_DATA


# ==========================================================================
# P2: direction_model
# ==========================================================================


def test_direction_model_gating_and_learning():
    import random

    from direction_model import MIN_MARKETS_PREDICT, DirectionModel

    m = DirectionModel(per_combo_min=999)  # per-combo devrede degil -> shared
    ck = _AH.key

    def mk(sign, rng):
        return _fv(
            ret_slow=0.001 * sign + rng.gauss(0, 0.0001),
            ret_fast=0.0005 * sign,
            sign_persistence=0.8,
            flow_mid=0.5 * sign + rng.gauss(0, 0.02),
            flow_fast=0.4 * sign,
            distance_bps=5.0 * sign + rng.gauss(0, 0.2),
            ptb_z=1.0 * sign,
            rv_fast=0.001, rv_slow=0.001, vol_percentile=0.5,
        )

    rng = random.Random(1)
    # esikten once: hazir degil
    m.learn_with_label(ck, [mk(1, rng)], 1)
    assert m.predict(ck, mk(1, rng)).ready is False

    # yeterli market ogret (UP=1 pozitif fv, DOWN=0 negatif fv)
    for _ in range(MIN_MARKETS_PREDICT + 3):
        m.learn_with_label(ck, [mk(1, rng), mk(1, rng)], 1)
        m.learn_with_label(ck, [mk(-1, rng), mk(-1, rng)], 0)

    up = m.predict(ck, mk(1, rng))
    dn = m.predict(ck, mk(-1, rng))
    assert up.ready is True and up.p_up is not None
    assert up.p_up > 0.5 > dn.p_up  # ayrisma ogrenildi
    assert up.p_up_no_clob is not None  # CLOB'suz varyant da egitildi


# ==========================================================================
# P2: calibration
# ==========================================================================


def test_calibration_honesty_and_accuracy():
    from calibration import CalibrationBook, CalSample

    book = CalibrationBook(min_n=5)
    ck = _AH.key
    # 4 ornek -> yetersiz
    for _ in range(4):
        book.record(ck, CalSample(decided=True, outcome_up=True, p_up=0.7,
                                   decision_up=True, confidence=0.4, market_implied_up=0.5))
    assert book.summary()["overall"]["insufficient"] is True

    # topla: 6 dogru UP tahmini -> yeterli, accuracy 1.0
    for _ in range(2):
        book.record(ck, CalSample(decided=True, outcome_up=True, p_up=0.7,
                                   decision_up=True, confidence=0.4, market_implied_up=0.5))
    s = book.summary()["overall"]
    assert s["insufficient"] is False
    assert s["accuracy"] == pytest.approx(1.0)
    assert s["price_edge"]["mean_edge"] == pytest.approx(0.2)


# ==========================================================================
# P3: train_offline (walk-forward, reconstruction, metrics, full report)
# ==========================================================================


def test_walk_forward_no_market_leakage():
    from train_offline import MarketData, walk_forward_folds

    markets = [
        MarketData(condition_id=f"m{i:03d}", combo_key="BTC:5m", start_ts=float(i), label_up=i % 2)
        for i in range(40)
    ]
    folds = walk_forward_folds(markets, n_folds=4)
    assert len(folds) >= 2
    for train, test in folds:
        tr_ids = {m.condition_id for m in train}
        te_ids = {m.condition_id for m in test}
        assert tr_ids.isdisjoint(te_ids)  # ayni market ikisinde birden YOK
        # test kronolojik olarak train'den SONRA
        assert max(m.start_ts for m in train) < min(m.start_ts for m in test)


def test_feature_vector_reconstruction():
    from features import FeatureVector
    from train_offline import feature_vector

    feats = {n: 1.0 for n in FeatureVector._BASE_FIELDS}
    feats.update({n: 2.0 for n in FeatureVector._CLOB_FIELDS})
    base = feature_vector(feats, include_clob=False)
    full = feature_vector(feats, include_clob=True)
    assert len(base) == len(FeatureVector._BASE_FIELDS)
    assert len(full) == len(base) + len(FeatureVector._CLOB_FIELDS)
    assert full[len(base)] == 2.0  # ilk CLOB feature


def test_metrics_perfect():
    import numpy as np

    from train_offline import metrics

    y = np.array([1, 0, 1, 0])
    p = np.array([0.99, 0.01, 0.98, 0.02])
    m = metrics(y, p)
    assert m["accuracy"] == 1.0
    assert m["brier"] < 0.01


def _build_synth_db(path, n_markets=40):
    from features import FeatureVector
    from models import (
        Asset, AssetHorizon, Decision, FeatureSnapshot, Horizon, MarketRef, ResolutionType,
    )
    from recorder import Recorder

    combo = AssetHorizon(Asset.BTC, Horizon.H5M)
    rec = Recorder(path)

    def feats(sign):
        d = {n: 0.0 for n in FeatureVector._BASE_FIELDS + FeatureVector._CLOB_FIELDS}
        d["distance_bps"] = 5.0 * sign
        d["ptb_z"] = 1.0 * sign
        d["ret_slow"] = 0.001 * sign
        d["flow_mid"] = 0.5 * sign
        d["sign_persistence"] = 0.8
        d["obi"] = 0.3 * sign
        d["up_mid_vel"] = 0.01 * sign
        return d

    for i in range(n_markets):
        sign = 1 if i % 2 == 0 else -1
        cid = f"m{i:03d}"
        ref = MarketRef(
            combo=combo, condition_id=cid, slug=f"btc-updown-5m-{1000+i}",
            question="BTC Up?", up_token_id="u", down_token_id="d",
            start_ts=1000.0 + i, end_ts=1300.0 + i,
            resolution_source="Chainlink", resolution_type=ResolutionType.CHAINLINK,
        )
        rec.record_market(ref)
        for cp in (60, 30, 10):
            snap = FeatureSnapshot(
                combo=combo, ts=1000.0 + i, seconds_remaining=float(cp),
                up_mid=(0.6 if sign > 0 else 0.4),
            )
            snap.extra = feats(sign)
            rec.record_snapshot(ref, snap, cp)
        ref.resolved = True
        ref.resolved_outcome = Decision.UP if sign > 0 else Decision.DOWN
        rec.settle(ref)
    rec.close()


def test_build_report_insufficient(tmp_path):
    from train_offline import build_report

    db = str(tmp_path / "few.sqlite")
    _build_synth_db(db, n_markets=10)
    rep = build_report(db)
    assert rep["insufficient"] is True
    assert rep["n_resolved_markets"] == 10


def test_build_report_sufficient_walk_forward(tmp_path):
    from train_offline import build_report

    db = str(tmp_path / "many.sqlite")
    _build_synth_db(db, n_markets=40)
    rep = build_report(db)
    assert rep["insufficient"] is False
    assert rep["n_resolved_markets"] == 40
    assert "walk_forward" in rep["split"]
    mb = rep["model_B_with_clob"]
    assert mb["n_folds"] >= 1
    # ayrilabilir sentetik veri -> yuksek dogruluk (GERCEK iddia degil, test)
    assert mb["mean_accuracy"] >= 0.8
    # CLOB'suz varyant da uretildi
    assert rep["model_B_no_clob"]["n_folds"] >= 1


# ==========================================================================
# Fixler (canli panelden gorulen hatalar): pencere secimi + spot tazelik
# ==========================================================================


def _ref(start, end, cid="x"):
    return MarketRef(
        combo=_AH, condition_id=cid, slug="btc-updown-5m-1",
        question="q", up_token_id="u", down_token_id="d",
        start_ts=start, end_ts=end,
        resolution_source="Chainlink", resolution_type=ResolutionType.CHAINLINK,
    )


def test_more_current_prefers_open_then_nearest():
    from discovery import _more_current

    now = 1000.0
    open_now = _ref(now - 100, now + 100, "open")      # su an acik
    far_future = _ref(now + 1000, now + 1300, "far")   # cok ileri pencere
    near_future = _ref(now + 10, now + 310, "near")     # yakin gelecek
    # acik pencere, ileri penceredem daha uygun
    assert _more_current(open_now, far_future, now) is True
    assert _more_current(far_future, open_now, now) is False
    # ikisi de gelecekse en yakin kapanan tercih
    assert _more_current(near_future, far_future, now) is True


def test_spot_price_prefers_fresher_source():
    from binance_feed import SymbolFeed
    from models import LocalBook
    import time as _t

    feed = SymbolFeed("SOLUSDT", 100)
    now_ms = int(_t.time() * 1000)
    # eski trade (3s once)
    feed.prices.append((now_ms - 3000, 76.0))
    # taze book (100ms once)
    feed.book = LocalBook("SOLUSDT", bids={75.9: 5.0}, asks={76.1: 5.0}, synced=True)
    feed.last_depth_ts_ms = now_ms - 100
    price, age = feed.spot_price()
    assert age is not None and age <= 200  # book-mid (taze) secildi
    assert price == pytest.approx(76.0)  # mid = (75.9+76.1)/2
