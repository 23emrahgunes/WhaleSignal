from __future__ import annotations

from dataclasses import replace

import pytest

from p26_calibration import (
    bucket_bounds,
    conservative_probability,
    ensure_calibration_schema,
    wilson_interval,
)
from p26_config import P26Settings
from p26_schema import connect_p26


def _settings(tmp_path, **updates):
    base = P26Settings(
        p25_db_path=str(tmp_path / "p25.sqlite"),
        p26_db_path=str(tmp_path / "p26.sqlite"),
        calibration_bucket_width=0.1,
        calibration_min_bucket_n=5,
    )
    return base.model_copy(update=updates)


def _insert(conn, *, cid, ts, combo, horizon, p, y, version="P26_FAIR_VALUE_V1"):
    conn.execute(
        """
        INSERT INTO p26_oos_predictions(
            condition_id,fold_id,decision_ts_ms,combo_key,horizon,p_up_raw,
            official_label,market_p_up,selected_c,role,model_version,created_at_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,'OUTER_TEST',?,?)
        """,
        (cid, "fold-1", ts, combo, horizon, p, y, 0.5, 1.0, version, ts + 1),
    )


def test_wilson_interval_known_properties():
    interval = wilson_interval(50, 100, 1.96)
    assert interval.lower == pytest.approx(0.4038, abs=0.002)
    assert interval.upper == pytest.approx(0.5962, abs=0.002)
    assert wilson_interval(0, 0).lower is None
    assert wilson_interval(0, 10).lower == pytest.approx(0.0)
    assert wilson_interval(10, 10).upper == pytest.approx(1.0)
    with pytest.raises(ValueError):
        wilson_interval(11, 10)


def test_bucket_bounds_are_fixed_and_bounded():
    assert bucket_bounds(0.66, 0.1) == pytest.approx((0.6, 0.7))
    assert bucket_bounds(1.0, 0.1) == pytest.approx((0.9, 1.0))


def test_combo_bucket_and_side_specific_down_lower(tmp_path):
    settings = _settings(tmp_path)
    conn = connect_p26(settings.p26_db_path)
    ensure_calibration_schema(conn)
    for i, y in enumerate([1, 1, 1, 1, 0, 1]):
        _insert(conn, cid=f"btc-{i}", ts=100 + i, combo="BTC:5m", horizon="5m", p=0.66, y=y)
    conn.commit()
    result = conservative_probability(
        conn,
        settings,
        p_up_raw=0.66,
        combo_key="BTC:5m",
        horizon="5m",
        cutoff_ts_ms=1000,
    )
    assert result.ready
    assert result.scope == "PER_COMBO"
    assert result.bucket_n == 6
    interval = wilson_interval(5, 6, settings.calibration_z)
    assert result.p_lower_up == pytest.approx(interval.lower)
    assert result.p_lower_down == pytest.approx(1.0 - interval.upper)
    conn.close()


def test_scope_fallback_combo_to_horizon(tmp_path):
    settings = _settings(tmp_path)
    conn = connect_p26(settings.p26_db_path)
    ensure_calibration_schema(conn)
    for i in range(6):
        _insert(conn, cid=f"eth-{i}", ts=100+i, combo="ETH:5m", horizon="5m", p=0.64, y=int(i < 4))
    conn.commit()
    result = conservative_probability(
        conn,
        settings,
        p_up_raw=0.64,
        combo_key="BTC:5m",
        horizon="5m",
        cutoff_ts_ms=1000,
    )
    assert result.ready
    assert result.scope == "HORIZON"
    conn.close()


def test_no_future_oos_row_can_enter_current_bucket(tmp_path):
    settings = _settings(tmp_path, calibration_min_bucket_n=3)
    conn = connect_p26(settings.p26_db_path)
    ensure_calibration_schema(conn)
    for i in range(3):
        _insert(conn, cid=f"past-{i}", ts=100+i, combo="BTC:5m", horizon="5m", p=0.66, y=1)
    for i in range(10):
        _insert(conn, cid=f"future-{i}", ts=2000+i, combo="BTC:5m", horizon="5m", p=0.66, y=0)
    conn.commit()
    result = conservative_probability(
        conn,
        settings,
        p_up_raw=0.66,
        combo_key="BTC:5m",
        horizon="5m",
        cutoff_ts_ms=1000,
    )
    assert result.bucket_n == 3
    assert result.bucket_wins == 3
    assert result.history_max_ts_ms == 102
    conn.close()


def test_insufficient_bucket_fails_closed(tmp_path):
    settings = _settings(tmp_path, calibration_min_bucket_n=10)
    conn = connect_p26(settings.p26_db_path)
    ensure_calibration_schema(conn)
    for i in range(4):
        _insert(conn, cid=f"row-{i}", ts=i, combo="BTC:5m", horizon="5m", p=0.7, y=i % 2)
    conn.commit()
    result = conservative_probability(
        conn,
        settings,
        p_up_raw=0.7,
        combo_key="BTC:5m",
        horizon="5m",
        cutoff_ts_ms=100,
    )
    assert not result.ready
    assert result.source == "INSUFFICIENT_OOS_BUCKET"
    conn.close()
