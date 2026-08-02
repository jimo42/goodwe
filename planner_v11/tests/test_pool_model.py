"""Testy pro lib/pool_model.py - hermetické.

Spouštět na serveru: cd planner_v10 && python3 tests/run_manual.py
(pytest NENÍ na serveru nainstalován - viz HANDOFF_v10.md).
"""
import copy
import os
import sys
import tomllib
from datetime import date, datetime, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import pool_model as pm  # noqa: E402
from lib import load_model as lm  # noqa: E402
from lib.config import parse_config_dict  # noqa: E402

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.toml")


def _raw_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def _cfg(overrides: dict | None = None):
    raw = copy.deepcopy(_raw_config())
    if overrides:
        for section, kv in overrides.items():
            raw.setdefault(section, {}).update(kv)
    return parse_config_dict(raw)


def test_morning_and_afternoon_window_from_config():
    cfg = _cfg()
    mw = pm.morning_window(cfg)
    aw = pm.afternoon_window(cfg)
    assert mw.start == time(9, 30)
    assert mw.end == time(12, 30)
    assert aw.start == time(13, 30)
    assert aw.end == time(16, 30)


def test_is_in_any_window():
    cfg = _cfg()
    assert pm.is_in_any_window(datetime(2026, 7, 21, 10, 0), cfg) is True
    assert pm.is_in_any_window(datetime(2026, 7, 21, 14, 0), cfg) is True
    assert pm.is_in_any_window(datetime(2026, 7, 21, 8, 0), cfg) is False
    assert pm.is_in_any_window(datetime(2026, 7, 21, 18, 0), cfg) is False


def test_circulation_load_kwh_for_slot_inside_and_outside_window():
    cfg = _cfg()
    inside = pm.circulation_load_kwh_for_slot(datetime(2026, 7, 21, 10, 0), 15, cfg)
    outside = pm.circulation_load_kwh_for_slot(datetime(2026, 7, 21, 20, 0), 15, cfg)
    assert abs(inside - cfg.pool.flow_power_kw * 0.25) < 1e-9
    assert outside == 0.0


def test_heat_pump_kw_at_clips_to_0_2_range():
    cfg = _cfg()
    # heat_pump_phase == "L1" (dle config.toml), zadame hodnotu nad 2.0
    s = lm.RawSample(
        timestamp=datetime(2026, 7, 21, 10, 0),
        load_l1_kw=5.0, load_l2_kw=0.0, load_l3_kw=0.0,
        house_consumption_kw=5.0,
    )
    kw = pm._heat_pump_kw_at(s, cfg)
    assert kw <= pm.HEAT_PUMP_PROFILE_MAX_KW
    assert kw >= pm.HEAT_PUMP_PROFILE_MIN_KW


def test_build_heat_pump_cycle_profile_relative_offsets():
    cfg = _cfg()
    d = date(2026, 7, 20)
    cycle_start = time(9, 30)
    samples = [
        lm.RawSample(
            timestamp=datetime(2026, 7, 20, 9, 30),
            load_l1_kw=1.0, load_l2_kw=0.0, load_l3_kw=0.0,
            house_consumption_kw=1.0,
        ),
        lm.RawSample(
            timestamp=datetime(2026, 7, 20, 9, 45),
            load_l1_kw=1.5, load_l2_kw=0.0, load_l3_kw=0.0,
            house_consumption_kw=1.5,
        ),
    ]
    profile = pm.build_heat_pump_cycle_profile({d: samples}, cycle_start, cfg)
    assert 0 in profile
    assert 15 in profile
    assert abs(profile[0] - 1.0) < 1e-6
    assert abs(profile[15] - 1.5) < 1e-6


def test_weighted_heat_pump_forecast_kw_uses_configured_weights():
    cfg = _cfg()
    # profil vcerejsi=2.0, predvcerejsi=4.0, treti=6.0 na minute_offset=0
    profiles = [{0: 2.0}, {0: 4.0}, {0: 6.0}]
    result = pm.weighted_heat_pump_forecast_kw(profiles, 0, cfg)
    w = cfg.pool.profile_weights
    expected = 2.0 * w[0] + 4.0 * w[1] + 6.0 * w[2]
    assert abs(result - expected) < 1e-6


def test_weighted_heat_pump_forecast_kw_rescales_when_day_missing():
    cfg = _cfg()
    # jen vcerejsi den dostupny, ostatni None
    profiles = [{0: 3.0}, None, None]
    result = pm.weighted_heat_pump_forecast_kw(profiles, 0, cfg)
    assert abs(result - 3.0) < 1e-6


def test_weighted_heat_pump_forecast_kw_no_data_returns_zero():
    result = pm.weighted_heat_pump_forecast_kw([None, None, None], 0, _cfg())
    assert result == 0.0


def test_circulation_detected_in_window_true_and_false():
    cfg = _cfg()
    window = pm.morning_window(cfg)
    samples_with = [
        lm.RawSample(
            timestamp=datetime(2026, 7, 21, 10, 0),
            load_l1_kw=0.0, load_l2_kw=0.0, load_l3_kw=0.55,
            house_consumption_kw=0.55,
        ),
    ]
    samples_without = [
        lm.RawSample(
            timestamp=datetime(2026, 7, 21, 10, 0),
            load_l1_kw=0.0, load_l2_kw=0.0, load_l3_kw=0.0,
            house_consumption_kw=0.0,
        ),
    ]
    assert pm.circulation_detected_in_window(samples_with, window, cfg) is True
    assert pm.circulation_detected_in_window(samples_without, window, cfg) is False


def _obs(**kwargs) -> pm.DayCycleObservation:
    defaults = dict(
        date=date(2026, 7, 21),
        morning_cycle_seen=True,
        afternoon_cycle_seen=True,
        morning_window_minutes_elapsed_without_signature=0.0,
        afternoon_window_minutes_elapsed_without_signature=0.0,
        run_outside_windows_detected=False,
        run_duration_exceeds_window_by_minutes=0.0,
    )
    defaults.update(kwargs)
    return pm.DayCycleObservation(**defaults)


def test_classify_state_normal_operation():
    cfg = _cfg()
    state, reason = pm.classify_state(
        pm.STATE_NORMAL_OPERATION, _obs(), 0, 1, cfg
    )
    assert state == pm.STATE_NORMAL_OPERATION
    assert reason == "POOL_EXPECTED_LOAD"


def test_classify_state_continuous_override():
    cfg = _cfg()
    obs = _obs(run_outside_windows_detected=True)
    state, reason = pm.classify_state(pm.STATE_NORMAL_OPERATION, obs, 0, 1, cfg)
    assert state == pm.STATE_CONTINUOUS_OVERRIDE
    assert reason == "POOL_CONTINUOUS_OVERRIDE"


def test_classify_state_expected_cycle_missing():
    cfg = _cfg()
    obs = _obs(
        morning_cycle_seen=False,
        morning_window_minutes_elapsed_without_signature=35.0,
    )
    state, reason = pm.classify_state(pm.STATE_NORMAL_OPERATION, obs, 0, 1, cfg)
    assert state == pm.STATE_EXPECTED_CYCLE_MISSING
    assert reason == "POOL_CYCLE_MISSING"


def test_classify_state_off_season_after_missing_both_cycles():
    cfg = _cfg()
    obs = _obs(
        morning_cycle_seen=False, afternoon_cycle_seen=False,
        morning_window_minutes_elapsed_without_signature=0.0,
        afternoon_window_minutes_elapsed_without_signature=0.0,
    )
    state, reason = pm.classify_state(
        pm.STATE_NORMAL_OPERATION, obs, consecutive_days_without_pool=1,
        consecutive_days_with_stable_run=0, cfg=cfg,
    )
    assert state == pm.STATE_OFF_SEASON
    assert reason == "POOL_SEASON_STOPPED"


def test_classify_state_season_started_from_off_season():
    cfg = _cfg()
    obs = _obs()
    state, reason = pm.classify_state(
        pm.STATE_OFF_SEASON, obs, consecutive_days_without_pool=0,
        consecutive_days_with_stable_run=1, cfg=cfg,
    )
    assert state == pm.STATE_NORMAL_OPERATION
    assert reason == "POOL_SEASON_STARTED"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
