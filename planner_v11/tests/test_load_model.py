"""Testy pro lib/load_model.py - hermetické, žádná závislost na reálných
datech na disku.

Spouštět na serveru: cd planner_v10 && python3 tests/run_manual.py
(pytest NENÍ na serveru nainstalován - viz HANDOFF_v10.md).
"""
import copy
import os
import sys
import tomllib
from datetime import date, datetime, timedelta


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


_SAMPLE_TEXT = """timestamp: \t\t Timestamp = 2026-07-21 12:34:56
load_p1: \t\t Load L1 = 300 W
load_p2: \t\t Load L2 = 250 W
load_p3: \t\t Load L3 = 550 W
house_consumption: \t\t House Consumption = 1100 W
"""


def test_parse_report_text_basic():
    out = lm.parse_report_text(_SAMPLE_TEXT)
    assert out["timestamp"] == "2026-07-21 12:34:56"
    assert out["load_p1"] == "300 W"
    assert out["house_consumption"] == "1100 W"


def test_extract_sample_basic():
    raw = lm.parse_report_text(_SAMPLE_TEXT)
    s = lm.extract_sample(raw)
    assert s is not None
    assert s.timestamp == datetime(2026, 7, 21, 12, 34, 56)
    assert abs(s.load_l1_kw - 0.3) < 1e-9
    assert abs(s.load_l3_kw - 0.55) < 1e-9
    assert abs(s.house_consumption_kw - 1.1) < 1e-9


def test_extract_sample_missing_field_returns_none():
    raw = {"timestamp": "2026-07-21 12:34:56", "load_p1": "300 W"}
    assert lm.extract_sample(raw) is None


def test_read_report_file_missing_returns_none():
    assert lm.read_report_file("/nonexistent/report/file") is None


def test_estimate_exclusion_pool_circulation_detected():
    cfg = _cfg()
    # cfg.pool.flow_phase == "L3", flow_power_kw == 0.55
    s = lm.RawSample(
        timestamp=datetime(2026, 7, 21, 10, 0),
        load_l1_kw=0.2, load_l2_kw=0.2, load_l3_kw=0.55,
        house_consumption_kw=0.95,
    )
    exclusion = lm.estimate_exclusion(s, cfg)
    assert abs(exclusion.pool_circulation_kw - 0.55) < 1e-6
    assert exclusion.ev_kw == 0.0
    assert exclusion.boiler_kw == 0.0


def test_estimate_exclusion_ev_detected():
    cfg = _cfg()
    # cfg.ev.phase == "L1", start residual threshold 2.4 kW
    s = lm.RawSample(
        timestamp=datetime(2026, 7, 21, 10, 0),
        load_l1_kw=2.75, load_l2_kw=0.1, load_l3_kw=0.1,
        house_consumption_kw=2.95,
    )
    exclusion = lm.estimate_exclusion(s, cfg)
    assert exclusion.ev_kw > 0.0


def test_estimate_exclusion_boiler_detected():
    cfg = _cfg()
    # cfg.boiler.phase_power_kw == 2.0 - simulace 1 fáze sepnutá na L1
    s = lm.RawSample(
        timestamp=datetime(2026, 7, 21, 10, 0),
        load_l1_kw=2.0, load_l2_kw=0.1, load_l3_kw=0.1,
        house_consumption_kw=2.2,
    )
    exclusion = lm.estimate_exclusion(s, cfg)
    assert abs(exclusion.boiler_kw - 2.0) < 1e-6


def test_excluded_load_kw_subtracts_and_floors_at_zero():
    cfg = _cfg()
    s = lm.RawSample(
        timestamp=datetime(2026, 7, 21, 10, 0),
        load_l1_kw=0.0, load_l2_kw=0.0, load_l3_kw=0.55,
        house_consumption_kw=0.55,
    )
    result = lm.excluded_load_kw(s, cfg)
    assert abs(result - 0.0) < 1e-6


def test_slot_key_for_weekday_and_weekend():
    # 2026-07-21 je utery (weekday)
    dt_weekday = datetime(2026, 7, 21, 8, 37)
    key = lm.slot_key_for(dt_weekday, 15)
    assert key == "weekday_08:30"
    # 2026-07-25 je sobota (weekend)
    dt_weekend = datetime(2026, 7, 25, 8, 37)
    key2 = lm.slot_key_for(dt_weekend, 15)
    assert key2 == "weekend_08:30"


def test_build_profile_basic_percentiles():
    cfg = _cfg()
    samples = []
    # 2026-07-01 je streda (weekday) - pouzij 5 tydnu po sobe (odstup 7 dni),
    # aby vsechny vzorky spolehlive spadly do STEJNEHO dne v tydnu bez
    # zavislosti na tom, jak presne po sobe jdouci kalendarni dny vychazeji.
    base_day = date(2026, 7, 1)
    samples = []
    for week_offset, value in zip(range(5), [1.0, 2.0, 3.0, 4.0, 5.0]):
        d = base_day + timedelta(days=7 * week_offset)
        samples.append(lm.RawSample(
            timestamp=datetime(d.year, d.month, d.day, 8, 0),
            load_l1_kw=0.0, load_l2_kw=0.0, load_l3_kw=0.0,
            house_consumption_kw=value,
        ))
    profile = lm.build_profile(
        samples, cfg, reference_date=base_day + timedelta(days=60), history_days=90,
    )
    key = "weekday_08:00"
    assert key in profile
    assert profile[key].sample_count == 5
    assert abs(profile[key].expected_kw - 3.0) < 1e-6  # P50 medián
    assert profile[key].reserve_kw > profile[key].expected_kw  # P75 > P50



def test_expected_load_kwh_for_slot_uses_fallback_when_insufficient_samples():
    profile: dict = {}
    dt = datetime(2026, 1, 1, 3, 0)
    expected, reserve, source = lm.expected_load_kwh_for_slot(
        profile, dt, slot_minutes=15, fallback_overnight_reserve_kw=0.46,
    )
    assert source == "fallback_overnight_reserve"
    assert abs(expected - 0.46 * 0.25) < 1e-6
    assert abs(reserve - expected) < 1e-9


def test_expected_load_kwh_for_slot_uses_profile_when_sufficient_samples():
    profile = {
        "weekday_08:00": lm.SlotProfile(expected_kw=1.5, reserve_kw=2.0, sample_count=10),
    }
    dt = datetime(2026, 7, 21, 8, 5)  # utery = weekday
    expected, reserve, source = lm.expected_load_kwh_for_slot(
        profile, dt, slot_minutes=15, fallback_overnight_reserve_kw=0.46,
    )
    assert source == "profile"
    assert abs(expected - 1.5 * 0.25) < 1e-6
    assert abs(reserve - 2.0 * 0.25) < 1e-6


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
