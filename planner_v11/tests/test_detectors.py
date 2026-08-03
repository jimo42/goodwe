"""Hermetic tests for live load detector helpers."""

import copy
import os
import sys
import tomllib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import detectors  # noqa: E402
from lib.config import parse_config_dict  # noqa: E402

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.toml")


def _cfg():
    with open(CONFIG_PATH, "rb") as f:
        return parse_config_dict(copy.deepcopy(tomllib.load(f)))


def _slot(now, **overrides):
    base = {
        "slot_start": now.isoformat(),
        "pool_load_kwh": 0.0,
        "ev_load_kwh": 0.0,
        "additional_load_kwh": 0.0,
        "boiler_power_kw": 0.0,
    }
    base.update(overrides)
    return base


def test_ev_requires_repeated_samples_before_accounting():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 18, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    live = {"house_consumption": 3200, "load_p1": 3010, "load_p2": 100, "load_p3": 90}
    first = detectors.detect_loads(now=now, cfg=cfg, live_state=live, current_slot=_slot(now))
    assert first["ev"]["detected_kw"] == 0.0
    assert first["ev"]["state"] == "CANDIDATE_WAITING_FOR_CONFIRMATION"

    history = [{"detected_loads": {"ev": {"detected_kw": 3.01}}} for _ in range(2)]
    confirmed = detectors.detect_loads(
        now=now + timedelta(minutes=10),
        cfg=cfg,
        live_state=live,
        current_slot=_slot(now),
        recent_history=history,
    )
    assert confirmed["ev"]["detected_kw"] > 2.9
    assert "EV_DETECTED" in confirmed["reason_codes"]


def test_ev_wallbox_power_is_detected_immediately_without_history():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 18, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    live = {"house_consumption": 3200, "load_p1": 3010, "load_p2": 100, "load_p3": 90}
    wallbox = {
        "available": True,
        "charging_power_w": 3069.0,
        "charging_power_kw": 3.069,
        "charging_energy_kwh": 2.95,
        "source": "salia.chargedata",
    }
    detected = detectors.detect_loads(
        now=now,
        cfg=cfg,
        live_state=live,
        current_slot=_slot(now),
        wallbox_state=wallbox,
    )
    assert detected["ev"]["detected_kw"] > 3.0
    assert detected["ev"]["source"] == "wallbox_api"
    assert detected["ev"]["confidence"] == 1.0
    assert "EV_DETECTED" in detected["reason_codes"]


def test_boiler_and_pool_are_subtracted_from_unexpected_load():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    slot = _slot(now, pool_load_kwh=cfg.pool.flow_power_kw * 0.25, boiler_power_kw=2.0)
    live = {"house_consumption": 2550, "load_p1": 2000, "load_p2": 0, "load_p3": 550}
    detected = detectors.detect_loads(now=now, cfg=cfg, live_state=live, current_slot=slot)
    assert detected["boiler"]["detected_kw"] == 2.0
    assert detected["pool"]["detected_kw"] >= 0.55
    assert detected["unexpected_load"]["kw"] == 0.0


def test_confirmed_boiler_mask_is_preferred_over_live_heuristic():
    cfg = _cfg()
    now = datetime(2026, 8, 3, 11, 38, tzinfo=ZoneInfo(cfg.system.timezone))
    live = {"house_consumption": 3791, "load_p1": 633, "load_p2": 2262, "load_p3": 642}
    slot = _slot(now, additional_load_kwh=0.624 * 0.25, boiler_power_kw=6.0)
    detected = detectors.detect_loads(
        now=now,
        cfg=cfg,
        live_state=live,
        current_slot=slot,
        boiler_ledger={"current_mask": [True, True, True]},
    )
    assert detected["boiler"]["source"] == "ledger_confirmed_mask"
    assert detected["boiler"]["detected_kw"] == 6.0
    assert detected["boiler"]["phases"] == {"phase1": True, "phase2": True, "phase3": True}
    assert detected["unexpected_load"]["active"] is False
    assert detected["unexpected_load"]["kw"] < 0.5


def test_confirmed_boiler_telemetry_is_preferred_over_ledger_mask():
    cfg = _cfg()
    now = datetime(2026, 8, 3, 12, 58, tzinfo=ZoneInfo(cfg.system.timezone))
    live = {"house_consumption": 4886, "load_p1": 138, "load_p2": 2354, "load_p3": 2071}
    detected = detectors.detect_loads(
        now=now,
        cfg=cfg,
        live_state=live,
        current_slot=_slot(now),
        boiler_ledger={"current_mask": [True, True, True]},
        telemetry_evidence={"confirmed_phase_delivery_kw": [0.0, 0.0, 1.961]},
    )
    assert detected["boiler"]["source"] == "telemetry_confirmed_phase_delivery"
    assert detected["boiler"]["detected_kw"] == 1.961
    assert detected["boiler"]["phase_kw"] == {"phase1": 0.0, "phase2": 0.0, "phase3": 1.961}
    assert detected["boiler"]["phases"] == {"phase1": False, "phase2": False, "phase3": True}


def test_unexpected_load_becomes_active_after_two_samples():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 18, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    live = {"house_consumption": 2600, "load_p1": 2600, "load_p2": 0, "load_p3": 0}
    history = [{"detected_loads": {"unexpected_load": {"kw": 2.6}}}]
    detected = detectors.detect_loads(
        now=now,
        cfg=cfg,
        live_state=live,
        current_slot=_slot(now),
        recent_history=history,
    )
    assert detected["unexpected_load"]["active"] is True
    assert detected["unexpected_load"]["replan_recommended"] is True
    assert detected["planning_adjustment"]["kw"] >= 2.6


def test_small_watt_phase_loads_are_not_treated_as_kw():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 18, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    live = {"house_consumption": 120, "load_p1": 25, "load_p2": 40, "load_p3": 55}
    detected = detectors.detect_loads(now=now, cfg=cfg, live_state=live, current_slot=_slot(now))
    assert detected["measured_house_kw"] == 0.12
    assert detected["pool"]["detected_kw"] == 0.0
    assert detected["unexpected_load"]["kw"] < 0.2
    assert detected["unexpected_load"]["active"] is False


def test_ev_fallback_counts_candidate_phase_load_history():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 18, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    live = {"house_consumption": 3200, "load_p1": 3010, "load_p2": 100, "load_p3": 90}
    history = [
        {"detected_loads": {"ev": {"detected_kw": 0.0, "phase_load_kw": 3.01, "state": "CANDIDATE_WAITING_FOR_CONFIRMATION"}}},
        {"detected_loads": {"ev": {"detected_kw": 0.0, "phase_load_kw": 3.02, "state": "CANDIDATE_WAITING_FOR_CONFIRMATION"}}},
    ]
    detected = detectors.detect_loads(
        now=now + timedelta(minutes=15),
        cfg=cfg,
        live_state=live,
        current_slot=_slot(now),
        recent_history=history,
        wallbox_state={"available": False, "error": "test unavailable"},
    )
    assert detected["ev"]["source"] == "phase_load_heuristic"
    assert detected["ev"]["detected_kw"] > 2.9
    assert detected["unannounced_ev_load"]["active"] is True
    assert detected["unannounced_ev_load"]["assumed_total_kwh"] == 8.0


def test_unexpected_load_clears_when_current_sample_is_below_threshold():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 18, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    live = {"house_consumption": 300, "load_p1": 120, "load_p2": 100, "load_p3": 80}
    history = [{"detected_loads": {"unexpected_load": {"kw": 2.6}}}]
    detected = detectors.detect_loads(
        now=now,
        cfg=cfg,
        live_state=live,
        current_slot=_slot(now),
        recent_history=history,
    )
    assert detected["unexpected_load"]["active"] is False
    assert detected["unexpected_load"]["replan_recommended"] is False


def test_implausibly_high_unexpected_load_is_flagged_not_confirmed():
    """A likely sensor glitch (e.g. mis-scaled W/kW reading) must not
    immediately confirm/replan; it should surface only as a data_quality flag.
    """
    cfg = _cfg()
    now = datetime(2026, 7, 22, 18, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    live = {"house_consumption": 29900, "load_p1": 29900, "load_p2": 0, "load_p3": 0}
    history = [
        {"detected_loads": {"unexpected_load": {"kw": 29.9}}},
        {"detected_loads": {"unexpected_load": {"kw": 29.9}}},
    ]
    detected = detectors.detect_loads(
        now=now,
        cfg=cfg,
        live_state=live,
        current_slot=_slot(now),
        recent_history=history,
    )
    assert detected["unexpected_load"]["active"] is False
    assert detected["unexpected_load"]["replan_recommended"] is False
    assert detected["data_quality"] == "SUSPICIOUS_HIGH_UNEXPECTED_LOAD_READING"
    assert "UNEXPECTED_LOAD_SUSPICIOUS_SANITY_CAP" in detected["reason_codes"]