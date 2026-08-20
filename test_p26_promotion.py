from __future__ import annotations

import math

from p26_config import P26Settings
from p26_eval import ensure_eval_schema
from p26_paper_v2_recorder import ensure_paper_v2_schema
from p26_promotion import evaluate_promotion, temporal_block_bootstrap
from p26_schema import connect_p26


def _settings(tmp_path, **updates):
    base=P26Settings(
        p25_db_path=str(tmp_path/"p25.sqlite"),
        p26_db_path=str(tmp_path/"p26.sqlite"),
        promotion_min_oos_markets=20,
        promotion_min_oos_class_markets=8,
        promotion_min_paper_trades=12,
        promotion_bootstrap_blocks=500,
        promotion_block_hours=1,
        promotion_min_positive_fold_fraction=0.5,
        promotion_max_asset_concentration=0.8,
        promotion_max_horizon_concentration=0.8,
        promotion_max_drawdown_fraction=0.5,
    )
    return base.model_copy(update=updates)


def _seed_predictions(conn,n=24,bad=False,concentrated=False):
    ensure_eval_schema(conn)
    assets=["BTC"] if concentrated else ["BTC","ETH","SOL","XRP"]
    horizons=["5m"] if concentrated else ["5m","15m","1h"]
    for i in range(n):
        y=i%2
        model=(0.1 if y==1 else 0.9) if bad else (0.85 if y==1 else 0.15)
        market=0.60 if y==1 else 0.40
        asset=assets[i%len(assets)]; horizon=horizons[i%len(horizons)]
        conn.execute(
            """INSERT INTO p26_oos_predictions(condition_id,fold_id,decision_ts_ms,combo_key,horizon,p_up_raw,official_label,market_p_up,selected_c,role,model_version,created_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,'OUTER_TEST','P26_FAIR_VALUE_V1',?)""",
            (f"c{i}",f"fold-{i//6}",i*3_600_000,f"{asset}:{horizon}",horizon,model,y,market,1.0,i*3_600_000+1),
        )
    conn.commit()


def _seed_paper(conn,n=16,positive=True):
    ensure_paper_v2_schema(conn)
    for i in range(n):
        correct=1 if positive or i%3 else 0
        stake=2.5
        pnl=0.5 if correct else -2.5
        conn.execute(
            """INSERT INTO p26_paper_trades(condition_id,combo_key,horizon,strategy_version,forecast_ts_ms,fill_ts_ms,side,status,reason,stake_usdc,diagnostics_json,official_result,correct,gross_payout,realized_pnl,roi,settled_at_ms,created_at_ms)
               VALUES (?,?,?,?,?,?,?,'SETTLED','OPEN',?,'{}','UP',?,?,?,?,?,?)""",
            (f"p{i}",f"{['BTC','ETH','SOL','XRP'][i%4]}:{['5m','15m','1h'][i%3]}",['5m','15m','1h'][i%3],"RESEARCH_PAPER_V2",i*3_600_000,i*3_600_000+100,"UP",stake,correct,3.0 if correct else 0.0,pnl,pnl/stake,i*3_600_000+1000,i*3_600_000),
        )
    conn.commit()


def test_not_ready_without_required_evidence(tmp_path):
    settings=_settings(tmp_path)
    conn=connect_p26(settings.p26_db_path); _seed_predictions(conn,n=4); conn.close()
    decision=evaluate_promotion(settings)
    assert decision.state=="NOT_READY"
    assert "OOS_MARKETS_INSUFFICIENT" in decision.reasons
    assert decision.safety["execution_enabled"] is False


def test_rejected_when_model_is_worse_than_market(tmp_path):
    settings=_settings(tmp_path)
    conn=connect_p26(settings.p26_db_path); _seed_predictions(conn,bad=True); _seed_paper(conn); conn.close()
    decision=evaluate_promotion(settings)
    assert decision.state=="REJECTED"
    assert "MODEL_DOES_NOT_BEAT_MARKET_BRIER" in decision.reasons


def test_validated_paper_model_with_strong_diverse_oos_and_pnl(tmp_path):
    settings=_settings(tmp_path,promotion_max_drawdown_fraction=1.0)
    conn=connect_p26(settings.p26_db_path); _seed_predictions(conn); _seed_paper(conn,positive=True); conn.close()
    decision=evaluate_promotion(settings)
    assert decision.state=="VALIDATED_PAPER_MODEL", decision.to_dict()
    assert decision.promoted
    assert decision.paper.pnl_interval.lower>0
    assert decision.predictive.paired_brier_delta.upper<0
    assert decision.safety["promotion_ceiling"]=="VALIDATED_PAPER_MODEL"


def test_concentration_rejects_otherwise_good_model(tmp_path):
    settings=_settings(tmp_path,promotion_max_asset_concentration=0.7,promotion_max_horizon_concentration=0.7,promotion_max_drawdown_fraction=1.0)
    conn=connect_p26(settings.p26_db_path); _seed_predictions(conn,concentrated=True); _seed_paper(conn); conn.close()
    decision=evaluate_promotion(settings)
    assert decision.state=="REJECTED"
    assert "ASSET_CONCENTRATION_TOO_HIGH" in decision.reasons
    assert "HORIZON_CONCENTRATION_TOO_HIGH" in decision.reasons


def test_temporal_block_bootstrap_is_deterministic_and_block_based():
    values=[(0,1.0),(100,1.0),(3_600_000,-1.0),(3_600_100,-1.0)]
    first=temporal_block_bootstrap(values,block_ms=3_600_000,samples=200,random_seed=7,statistic="sum")
    second=temporal_block_bootstrap(values,block_ms=3_600_000,samples=200,random_seed=7,statistic="sum")
    assert first==second
    assert first.samples==2
    assert first.point==0.0
