import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from p26_alpha_profile import FrozenAlphaProfile, save_alpha_profile
from p26_artifact import save_artifact
from p26_config import P26Settings
from p26_fair_value import new_champion
from p26_features import EXTERNAL_FEATURE_NAMES, schema_hash
from p26_oracle_store import OracleTick, OracleTickStore
from p26_paper_v2_daemon import PaperV2Runtime


def _p25(path: Path):
    conn=sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE markets(
      condition_id TEXT PRIMARY KEY,market_id TEXT,slug TEXT,combo_key TEXT,
      asset TEXT,horizon TEXT,market_start REAL,market_end REAL,
      official_result TEXT,official_result_source TEXT,official_resolved_at REAL,
      computed_result TEXT,label_status TEXT
    );
    CREATE TABLE snapshots(
      id INTEGER PRIMARY KEY AUTOINCREMENT,condition_id TEXT,combo_key TEXT,
      checkpoint_sec INTEGER,ts REAL,market_start REAL,market_end REAL,tte_sec REAL,
      extra_json TEXT,quality_status TEXT,source_age_ms REAL,book_age_ms REAL,
      clob_age_ms REAL,up_bid REAL,up_ask REAL,up_mid REAL,down_bid REAL,
      down_ask REAL,down_mid REAL,clob_spread REAL
    );
    """)
    end=1_800_000_000.0; start=end-300; decision=end-60+0.1
    conn.execute("INSERT INTO markets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(
        "cond","m","slug","BTC:5m","BTC","5m",start,end,
        None,None,None,None,"UNKNOWN"))
    features={name:0.1 for name in EXTERNAL_FEATURE_NAMES}
    features.update(feature_ready=True,feature_coverage=1.0,missing_features=[])
    conn.execute("""
      INSERT INTO snapshots(condition_id,combo_key,checkpoint_sec,ts,market_start,
      market_end,tte_sec,extra_json,quality_status,source_age_ms,book_age_ms,
      clob_age_ms,up_bid,up_ask,up_mid,down_bid,down_ask,down_mid,clob_spread)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,("cond","BTC:5m",60,decision,start,end,59.9,json.dumps(features),"CHOP",
           50,60,70,0.49,0.51,0.50,0.49,0.51,0.50,0.02))
    conn.commit(); conn.close()


def _settings(tmp_path: Path):
    return P26Settings(
        p25_db_path=str(tmp_path/"p25.sqlite"),
        p26_db_path=str(tmp_path/"p26.sqlite"),
        model_dir=str(tmp_path/"models"),
        paper_v2_model_manifest=str(tmp_path/"models"/"model.manifest.json"),
        paper_v2_alpha_artifact=str(tmp_path/"models"/"alpha.json"),
        paper_v2_enabled=True,
        canonical_max_lag_ms=2000,
    )


def _artifacts(settings: P26Settings):
    pipeline=new_champion(settings)
    X=np.asarray([[0.0]*len(EXTERNAL_FEATURE_NAMES),[1.0]*len(EXTERNAL_FEATURE_NAMES),
                  [0.2]*len(EXTERNAL_FEATURE_NAMES),[0.8]*len(EXTERNAL_FEATURE_NAMES)])
    y=np.asarray([0,1,0,1])
    pipeline.fit(X,y)
    loaded=save_artifact(
        pipeline=pipeline,output_dir=Path(settings.model_dir),stem="model",
        manifest_without_hash={
            "artifact_id":"model","artifact_version":settings.model_artifact_version,
            "created_at_utc":datetime.now(timezone.utc).isoformat(),"code_commit":"abc",
            "feature_schema_version":settings.feature_schema_version,
            "feature_schema_hash":schema_hash(EXTERNAL_FEATURE_NAMES,settings.feature_schema_version),
            "feature_names_in_exact_order":list(EXTERNAL_FEATURE_NAMES),
            "model_type":"LogisticRegression_L2","scaler_type":"RobustScaler_frozen",
            "imputer_type":"SimpleImputer_median_frozen","regularization":{"C":1.0},
            "random_seed":26,"training_cutoff_ms":100,
            "train_market_count":4,"train_up_count":2,"train_down_count":2,
            "train_condition_ids_sha256":"x","calibration_artifact_id":None,
        })
    assert loaded.manifest_path==Path(settings.paper_v2_model_manifest)
    save_alpha_profile(FrozenAlphaProfile(
        artifact_id="alpha",created_at_ms=100,code_commit="abc",
        source_model_version=settings.model_artifact_version,
        minimum_samples=30,buckets=(),
    ),Path(settings.paper_v2_alpha_artifact))


def test_runtime_is_fail_closed_and_records_mapping_not_ready_once(tmp_path):
    settings=_settings(tmp_path)
    _p25(Path(settings.p25_db_path))
    oracle=OracleTickStore(settings.p26_db_path)
    oracle.insert(OracleTick(
        asset="BTC",source="POLYMARKET_RTDS_CHAINLINK",value_text="100",
        value_real=100,source_ts_ms=1_799_999_939_000,
        recv_ts_ms=1_799_999_939_010,payload_sha256="tick"))
    oracle.close()
    from p26_dataset import CanonicalDatasetBuilder
    builder=CanonicalDatasetBuilder(settings,code_commit="abc")
    try:
        assert builder.sync().inserted==1
    finally:
        builder.close()
    _artifacts(settings)
    runtime=PaperV2Runtime(settings)
    try:
        first=runtime.process(now_ms=1_799_999_940_300)
        assert first["processed"]==1
        row=runtime.recorder.conn.execute("SELECT status,reason FROM p26_paper_trades").fetchone()
        assert row["status"]=="SKIPPED"
        assert row["reason"]=="TOKEN_MAPPING_NOT_READY"
        second=runtime.process(now_ms=1_799_999_940_300)
        assert second["processed"]==0
    finally:
        runtime.close()


def test_runtime_missing_artifacts_is_not_ready(tmp_path):
    settings=_settings(tmp_path)
    runtime=PaperV2Runtime(settings)
    try:
        result=runtime.process(now_ms=1000)
        assert result["status"]=="NOT_READY"
        assert result["reason"]=="MODEL_ARTIFACT_NOT_READY"
    finally:
        runtime.close()
