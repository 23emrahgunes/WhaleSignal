import json

from p26_alpha_train import build_profile
from p26_config import P26Settings
from p26_paper_v2_recorder import ensure_paper_v2_schema
from p26_schema import connect_p26


def test_alpha_profile_builder_uses_only_past_replays_and_quantile(tmp_path):
    db=str(tmp_path/"p26.sqlite")
    conn=connect_p26(db); ensure_paper_v2_schema(conn)
    for i,ttl in enumerate((300,500,700,900)):
        conn.execute("""
        INSERT INTO p26_alpha_replays(
          condition_id,strategy_version,combo_key,horizon,side,forecast_ts_ms,
          history_max_ts_ms,observations_json,missing_delays_json,
          initial_edge,last_edge,edge_retention_ratio,half_life_ms,
          time_to_zero_edge_ms,observation_count,created_at_ms)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,(f"c{i}","RESEARCH_PAPER_V2","BTC:5m","5m","UP",100+i,
              200+i,json.dumps([{"delay_ms":0,"net_edge":0.05}]),"[]",
              0.05,0.0,0.0,ttl/2,ttl,2,300+i))
    # Future row must not enter cutoff=1000? This one is after cutoff and excluded.
    conn.execute("""
        INSERT INTO p26_alpha_replays(
          condition_id,strategy_version,combo_key,horizon,side,forecast_ts_ms,
          history_max_ts_ms,observations_json,missing_delays_json,
          initial_edge,last_edge,edge_retention_ratio,half_life_ms,
          time_to_zero_edge_ms,observation_count,created_at_ms)
        VALUES('future','RESEARCH_PAPER_V2','BTC:5m','5m','UP',1500,1500,'[]','[]',0.1,0,0,1,10,1,1500)
    """)
    conn.commit(); conn.close()
    profile=build_profile(db,cutoff_ts_ms=1000,minimum_samples=3,quantile=0.25,
                          artifact_id="a",model_version="m")
    per_combo=[b for b in profile.buckets if b.scope=="PER_COMBO"][0]
    assert per_combo.sample_count==4
    assert 300 <= per_combo.ttl_ms <= 500
    assert per_combo.history_max_ts_ms==203
