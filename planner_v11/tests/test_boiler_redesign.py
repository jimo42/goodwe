"""Focused regression tests for planner_v11 boiler redesign."""
import copy
import importlib.util
import os
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import boiler_state, economics, relay, telemetry  # noqa: E402
from lib.config import parse_config_dict  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
spec = importlib.util.spec_from_file_location("executor_v11_boiler_tests", ROOT / "executor.py")
executor = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = executor
spec.loader.exec_module(executor)


def _cfg(overrides=None):
    with open(CONFIG_PATH, "rb") as handle:
        raw = copy.deepcopy(tomllib.load(handle))
    for section, values in (overrides or {}).items():
        raw.setdefault(section, {}).update(values)
    return parse_config_dict(raw)


def _sample(ts, *, export=1.0, phases=(0.5, 0.5, 0.5)):
    return telemetry.MinuteSample(ts, 5.0, sum(phases), export, phases, (0.0, 0.0, 0.0), "test")


def _write_report(path, ts, *, export=1000, phases=(500, 600, 700)):
    values = {
        "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "ppv1": "2000 W", "ppv2": "3000 W",
        "house_consumption": "1800 W", "meter_active_power_total": f"{export} W",
        "load_p1": f"{phases[0]} W", "load_p2": f"{phases[1]} W", "load_p3": f"{phases[2]} W",
        "meter_active_power1": "300 W", "meter_active_power2": "300 W", "meter_active_power3": "400 W",
    }
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(f"{key}: value = {value}" for key, value in values.items()))


def test_telemetry_uses_only_standard_reports_and_stable_export():
    tz = ZoneInfo("Europe/Prague")
    now = datetime(2026, 8, 2, 12, 5, tzinfo=tz)
    tmp = tempfile.mkdtemp(prefix="planner_v11_telemetry_")
    try:
        _write_report(os.path.join(tmp, "goodwe_stats_20260802_120100"), now - timedelta(minutes=4), export=4000)
        _write_report(os.path.join(tmp, "goodwe_stats_20260802_120200"), now - timedelta(minutes=3), export=3000)
        _write_report(os.path.join(tmp, "goodwe_stats_20260802_120300_full_"), now - timedelta(minutes=2), export=9000)
        live = {
            "ppv1": 2000, "ppv2": 3000, "house_consumption": 2000, "meter_active_power_total": 1000,
            "load_p1": 500, "load_p2": 600, "load_p3": 700,
            "meter_active_power1": 300, "meter_active_power2": 300, "meter_active_power3": 400,
        }
        samples = telemetry.load_recent_samples(now=now, live_state=live, reports_dir=tmp)
        evidence = telemetry.robust_evidence(samples, (False, False, False), 2.0, now=now)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert len(samples) == 3
    assert evidence["export_min_kw"] == 1.0
    assert evidence["export_median_kw"] == 3.0
    assert evidence["export_latest_kw"] == 1.0
    assert evidence["stable_export_kw"] == 1.0
    assert evidence["latest_age_seconds"] == 0.0


def test_ledger_distinguishes_commanded_and_thermostat_no_delivery():
    tz = ZoneInfo("Europe/Prague")
    now = datetime(2026, 8, 2, 12, 5, tzinfo=tz)
    state = boiler_state.empty_state()
    state["previous_executor_at"] = (now - timedelta(minutes=5)).isoformat()
    state["phase_baseline_kw"] = [0.5, 0.6, 0.7]
    samples = [_sample(now - timedelta(minutes=i), phases=(0.5, 0.6, 0.7)) for i in (4, 2, 0)]
    updated = boiler_state.update_energy_and_baselines(
        state, now=now, samples=samples, relay_mask=(True, False, False), phase_power_kw=2.0,
    )
    day = updated["days"][now.date().isoformat()]
    assert abs(day["commanded_kwh"] - (2.0 * 5 / 60)) < 1e-6
    assert day["estimated_delivered_kwh"] == 0.0
    assert day["delivery_confidence"] == "high"


def test_ledger_splits_commanded_energy_across_midnight():
    tz = ZoneInfo("Europe/Prague")
    now = datetime(2026, 8, 3, 0, 5, tzinfo=tz)
    state = boiler_state.empty_state()
    state["previous_executor_at"] = datetime(2026, 8, 2, 23, 55, tzinfo=tz).isoformat()
    updated = boiler_state.update_energy_and_baselines(
        state, now=now, samples=[], relay_mask=(True, False, False), phase_power_kw=2.0,
    )
    assert abs(updated["days"]["2026-08-02"]["commanded_kwh"] - 1 / 6) < 1e-6
    assert abs(updated["days"]["2026-08-03"]["commanded_kwh"] - 1 / 6) < 1e-6


def test_reconstructed_surplus_adds_only_confirmed_boiler_delivery():
    tz = ZoneInfo("Europe/Prague")
    now = datetime(2026, 8, 2, 12, 5, tzinfo=tz)
    samples = [_sample(now, export=1.0, phases=(2.5, 0.6, 0.7))]
    confirmed = telemetry.robust_evidence(
        samples, (True, False, False), 2.0, now=now, persisted_phase_baseline_kw=[0.5, 0.6, 0.7],
    )
    unknown = telemetry.robust_evidence(samples, (True, False, False), 2.0, now=now)
    assert confirmed["confirmed_boiler_delivery_kw"] == 2.0
    assert confirmed["reconstructed_pre_boiler_surplus_kw"] == 3.0
    assert unknown["confirmed_boiler_delivery_kw"] == 0.0
    assert unknown["reconstructed_pre_boiler_surplus_kw"] == 1.0


def test_economic_candidates_mix_surplus_and_import_and_future_pv_blocks_import():
    cfg = _cfg()
    now = datetime(2026, 8, 2, 10, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    slot = {"price_eur_mwh": -20.0}
    no_future = executor.economic_boiler_candidates(
        slot=slot, forecast_doc={"slots": []}, now=now, cfg=cfg,
        telemetry_evidence={"reconstructed_pre_boiler_surplus_kw": 3.0},
    )
    assert no_future["target_phases"] >= 1
    row = no_future["candidates"][2]
    assert row["surplus_covered_kw"] == 3.0 and row["import_covered_kw"] == 1.0

    future_slot = {
        "slot_start": (now + timedelta(hours=1)).isoformat(), "pv_estimate_kwh": 2.0,
        "fixed_load_kwh": 0.1, "export_revenue_czk_kwh": -1.0,
    }
    blocked = executor.economic_boiler_candidates(
        slot={"price_eur_mwh": 0.0}, forecast_doc={"slots": [future_slot]}, now=now, cfg=cfg,
        telemetry_evidence={"reconstructed_pre_boiler_surplus_kw": 1.0},
    )
    assert blocked["target_phases"] == 0
    assert blocked["candidates"][1]["future_solar_better_for_import"] is True


def test_phase_selection_least_loaded_hysteresis_minimum_times_and_headroom():
    cfg = _cfg()
    now = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    state = boiler_state.empty_state()
    mask, _, _ = executor.select_phase_mask(
        target_phases=2, current_mask=(False, False, False), baseline_kw=[3.0, 1.0, 2.0],
        ledger=state, now=now, cfg=cfg,
    )
    assert mask == (False, True, True)

    state["phase_last_on_at"] = [(now - timedelta(minutes=2)).isoformat(), None, None]
    held, reasons, _ = executor.select_phase_mask(
        target_phases=0, current_mask=(True, False, False), baseline_kw=[1.0, 1.0, 1.0],
        ledger=state, now=now, cfg=cfg,
    )
    assert held == (True, False, False) and "L1_MINIMUM_ON_HOLD" in reasons

    unsafe, _, _ = executor.select_phase_mask(
        target_phases=0, current_mask=(True, False, False), baseline_kw=[6.0, 1.0, 1.0],
        ledger=state, now=now, cfg=cfg,
    )
    assert unsafe == (False, False, False)

    hysteresis, reasons, _ = executor.select_phase_mask(
        target_phases=1, current_mask=(True, False, False), baseline_kw=[1.2, 1.0, 3.0],
        ledger=boiler_state.empty_state(), now=now, cfg=cfg,
    )
    assert hysteresis == (True, False, False)
    assert "BOILER_REBALANCE_SUPPRESSED_BY_HYSTERESIS" in reasons


def test_set_phase_empty_response_is_success_when_readback_matches():
    original_ip, original_http, original_read, original_sleep = relay._get_relay_ip, relay._http_get, relay.read_status_pole, relay.time.sleep
    reads = iter(["0000", "0100"])
    relay._get_relay_ip = lambda: "192.0.2.10"
    relay._http_get = lambda path, ip, timeout=5.0: None
    relay.read_status_pole = lambda: next(reads)
    relay.time.sleep = lambda seconds: None
    try:
        result = relay.set_phase("2", "ON", dry_run=False, verify_delay_s=0)
    finally:
        relay._get_relay_ip, relay._http_get, relay.read_status_pole, relay.time.sleep = original_ip, original_http, original_read, original_sleep
    assert result["status"] == "written_verified_by_readback"
    assert result["command_response_missing"] is True
    assert result["verified"] is True


def test_apply_mask_switches_off_before_on_and_verifies_final_mask():
    original_read, original_set = relay.read_status_pole, relay.set_phase
    statuses = iter(["0010", "0100"])
    actions = []
    relay.read_status_pole = lambda: next(statuses)
    relay.set_phase = lambda phase, state, **kwargs: actions.append((phase, state)) or {"verified": True, "relay_status_ok": True}
    try:
        result = relay.apply_phase_mask((False, True, False), dry_run=False, verify_delay_s=0)
    finally:
        relay.read_status_pole, relay.set_phase = original_read, original_set
    assert actions == [("1", "OFF"), ("2", "ON")]
    assert result["verified"] is True
    assert result["after_mask"] == [False, True, False]


def test_executor_rejects_unverified_adapter_result():
    cfg = _cfg({"system": {"dry_run": False, "boiler_write_enabled": True}})
    now = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    original = executor.execute_boiler_target
    executor.execute_boiler_target = lambda target, cfg_arg: {"status": "write_verification_failed", "relay_status_ok": True, "verified": False}
    try:
        decision = executor.decide_boiler_execution(
            {"price_eur_mwh": -100.0}, {"load_p1": 500, "load_p2": 600, "load_p3": 700}, cfg, True,
            relay_health={"relay_status_ok": True, "parsed": {"phase1": False, "phase2": False, "phase3": False}},
            forecast_doc={"slots": []}, now=now,
            telemetry_evidence={"reconstructed_pre_boiler_surplus_kw": 6.0, "robust_phase_baseline_kw": [0.5, 0.6, 0.7]},
        )
    finally:
        executor.execute_boiler_target = original
    assert decision["status"] == "relay_write_verification_failed"


def test_gas_heat_value_is_authoritative_economics_value():
    cfg = _cfg()
    assert economics.gas_heat_value_czk_per_kwh(cfg) == 2.46