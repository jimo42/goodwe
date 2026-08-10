"""Hermetické testy pro executor.py v10 tactical helpers."""

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

from lib.config import parse_config_dict  # noqa: E402

EXECUTOR_PATH = Path(__file__).resolve().parent.parent / "executor.py"
spec = importlib.util.spec_from_file_location("executor_v10_module", EXECUTOR_PATH)
executor = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = executor
spec.loader.exec_module(executor)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.toml")


def _cfg(overrides: dict | None = None):
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    raw = copy.deepcopy(raw)
    if overrides:
        for section, kv in overrides.items():
            raw.setdefault(section, {}).update(kv)
    return parse_config_dict(raw)


def _forecast(cfg, now):
    slot_start = executor.round_down_to_slot(now, cfg.system.planning_step_minutes)
    return {
        "schema_version": 10,
        "generated_at": (now - timedelta(minutes=5)).isoformat(),
        "valid_until": (now + timedelta(minutes=60)).isoformat(),
        "config_hash": cfg.config_hash,
        "model_version": "10-planner-v1",
        "solver": {"name": "PuLP+CBC", "status": "optimal"},
        "slots": [
            {
                "slot_start": slot_start.isoformat(),
                "battery_action": "HOLD",
                "battery_power_kw": 0.0,
                "boiler_power_kw": 4.0,
                "soc_start_pct": 50.0,
            }
        ],
    }


def test_validate_forecast_valid_and_stale():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    valid, reasons = executor.validate_forecast(_forecast(cfg, now), cfg, now)
    assert valid
    assert reasons == []

    stale = _forecast(cfg, now)
    stale["valid_until"] = (now - timedelta(minutes=1)).isoformat()
    valid, reasons = executor.validate_forecast(stale, cfg, now)
    assert not valid
    assert "FORECAST_STALE_VALID_UNTIL" in reasons


def test_find_current_slot():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 15, 7, tzinfo=ZoneInfo(cfg.system.timezone))
    slot = executor.find_current_slot(_forecast(cfg, now)["slots"], now, cfg)
    assert slot is not None
    assert slot["battery_action"] == "HOLD"


def test_safe_boiler_phases_by_headroom_limits_to_plan_and_current():
    cfg = _cfg()
    live = {"igrid1": 10.0, "igrid2": 10.0, "igrid3": 29.0}
    safe, reasons, evidence = executor.safe_boiler_phases_by_headroom(3, live, cfg)
    assert safe == 2
    assert "L3_HEADROOM_BLOCKED" in reasons
    assert evidence["L1"]["additional_current_a"] > 0


def test_decisions_blocked_by_dry_run_write_gates():
    cfg = _cfg({"system": {"dry_run": True, "battery_write_enabled": False, "boiler_write_enabled": False}})
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    slot = _forecast(cfg, now)["slots"][0]
    live = {"battery_soc": 50.0, "igrid1": 5.0, "igrid2": 5.0, "igrid3": 5.0}
    batt = executor.decide_battery_execution(_forecast(cfg, now), slot, cfg, forecast_valid=True, now=now)
    boil = executor.decide_boiler_execution(slot, live, cfg, forecast_valid=True)
    assert batt["execute"] is False
    assert batt["status"] == "blocked_by_dry_run_or_write_gate"
    assert boil["execute"] is False
    # Opportunistic planner target alone is no longer proof that five-minute
    # marginal heating is economic; without minute telemetry executor stays OFF.
    assert boil["target_phases"] == 0
    assert boil["status"] == "blocked_by_dry_run_or_write_gate"


def test_boiler_execution_uses_relay_adapter_after_write_gate(monkeypatch=None):
    cfg = _cfg({"system": {"dry_run": False, "boiler_write_enabled": True}})
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    slot = _forecast(cfg, now)["slots"][0]
    live = {"battery_soc": 50.0, "igrid1": 5.0, "igrid2": 5.0, "igrid3": 5.0}

    calls = []
    original = executor.execute_boiler_target
    executor.execute_boiler_target = lambda target, cfg_arg: calls.append((target, cfg_arg)) or {
        "status": "written",
        "relay_status_ok": True,
    }
    try:
        boil = executor.decide_boiler_execution(
            {**slot, "price_eur_mwh": 100.0},
            live,
            cfg,
            forecast_valid=True,
            forecast_doc={"slots": []},
            telemetry_evidence={
                "reconstructed_pre_boiler_surplus_kw": 4.0,
                "robust_phase_baseline_kw": [0.5, 0.6, 0.7],
            },
        )
    finally:
        executor.execute_boiler_target = original

    assert boil["execute"] is True
    assert boil["target_phases"] == 2
    assert boil["status"] == "relay_written"
    assert boil["adapter_result"]["status"] == "written"
    assert calls == [((True, True, False), cfg)]


def test_build_runtime_state_contract():
    cfg = _cfg({"system": {"dry_run": True, "battery_write_enabled": False, "boiler_write_enabled": False}})
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    forecast = _forecast(cfg, now)
    runtime = executor.build_runtime_state(
        now=now,
        cfg=cfg,
        forecast_doc=forecast,
        forecast_valid=True,
        forecast_reasons=[],
        current_slot=forecast["slots"][0],
        live_state={"battery_soc": 50.0},
        battery_decision={"status": "blocked_by_dry_run_or_write_gate"},
        boiler_decision={"status": "blocked_by_dry_run_or_write_gate"},
        relay_health={"status": "relay_status_ok", "relay_status_ok": True},
        detected_loads={"unexpected_load": {"active": False}},
        deviation_detected=False,
        deviation_reason="SOC_DEVIATION_OK_0.0_PCT_POINTS",
    )
    assert runtime["schema_version"] == 10
    assert runtime["forecast"]["valid"] is True
    assert runtime["current_slot_start"] == forecast["slots"][0]["slot_start"]
    assert runtime["write_gates"]["dry_run"] is True
    assert runtime["detected_loads"]["unexpected_load"]["active"] is False
    assert runtime["relay_health"]["relay_status_ok"] is True


def test_check_relay_health_reads_status_in_dry_run_context():
    from lib import relay

    original_read = relay.read_status_pole
    relay.read_status_pole = lambda: "1011"
    try:
        health = executor.check_relay_health()
    finally:
        relay.read_status_pole = original_read

    assert health["relay_status_ok"] is True
    assert health["parsed"]["phase1"] is True


def test_check_relay_health_marks_unavailable_status_as_fault():
    from lib import relay

    original_read = relay.read_status_pole
    relay.read_status_pole = lambda: None
    try:
        health = executor.check_relay_health()
    finally:
        relay.read_status_pole = original_read

    assert health["relay_status_ok"] is False
    assert health["status"] == "relay_status_read_failed"


def test_boiler_execution_blocks_real_write_when_relay_health_check_failed():
    cfg = _cfg({"system": {"dry_run": False, "boiler_write_enabled": True}})
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    slot = _forecast(cfg, now)["slots"][0]
    live = {"battery_soc": 50.0, "igrid1": 5.0, "igrid2": 5.0, "igrid3": 5.0}
    calls = []
    original = executor.execute_boiler_target
    executor.execute_boiler_target = lambda target, cfg_arg: calls.append((target, cfg_arg))
    try:
        decision = executor.decide_boiler_execution(
            slot,
            live,
            cfg,
            forecast_valid=True,
            relay_health={"relay_status_ok": False, "status": "relay_status_read_failed"},
        )
    finally:
        executor.execute_boiler_target = original

    assert decision["status"] == "blocked_relay_health_check_failed"
    assert decision["target_phases"] == 0
    assert decision["execute"] is False
    assert calls == []


def test_send_executor_alerts_for_relay_failure_and_unexpected_load():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    calls = []
    original = executor.alerting.notify.send
    executor.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
    tmp = tempfile.mkdtemp(prefix="planner_v10_test_executor_alerts_")
    try:
        outcomes = executor.send_executor_alerts(
            now=now,
            cfg=cfg,
            forecast_valid=True,
            forecast_reasons=[],
            boiler_decision={"status": "relay_write_verification_failed"},
            relay_health={"relay_status_ok": True},
            device_failures={"relay": {"consecutive_failures": 3}},
            detected_loads={"unexpected_load": {"active": True, "kw": 2.5, "dominant_phase": "L1", "started_at": now.isoformat()}},
            deviation_detected=True,
            deviation_reason="UNEXPECTED_LOAD_REPLAN",
            alert_state_path=Path(tmp) / "alert_state.json",
        )
    finally:
        executor.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)

    assert len(outcomes) == 2
    assert any("neočekávaná zátěž" in msg for msg in calls)
    assert any("relé bojleru" in msg for msg in calls)


def test_send_executor_alerts_suppresses_soc_deviation_in_dry_run():
    cfg = _cfg({"system": {"dry_run": True, "battery_write_enabled": False, "boiler_write_enabled": False}})
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    calls = []
    original = executor.alerting.notify.send
    executor.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
    tmp = tempfile.mkdtemp(prefix="planner_v10_test_executor_alerts_")
    try:
        outcomes = executor.send_executor_alerts(
            now=now,
            cfg=cfg,
            forecast_valid=True,
            forecast_reasons=[],
            boiler_decision={"status": "blocked_by_dry_run_or_write_gate"},
            relay_health={"relay_status_ok": True},
            detected_loads={"unexpected_load": {"active": False}},
            deviation_detected=True,
            deviation_reason="SOC_DEVIATION_ABOVE_16.0_PCT_POINTS",
            alert_state_path=Path(tmp) / "alert_state.json",
        )
    finally:
        executor.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)

    assert outcomes == []
    assert calls == []


def test_soc_deviation_threshold_and_direction():
    cfg = _cfg()
    assert cfg.alerts.soc_deviation_threshold_pct_points == 15.0
    slot = {"soc_start_pct": 50.0}

    detected, reason = executor.detect_plan_deviation(slot, {"battery_soc": 64.9}, cfg)
    assert detected is False
    assert reason == "SOC_DEVIATION_OK_ABOVE_14.9_PCT_POINTS"

    detected, reason = executor.detect_plan_deviation(slot, {"battery_soc": 65.0}, cfg)
    assert detected is True
    assert reason == "SOC_DEVIATION_ABOVE_15.0_PCT_POINTS"

    detected, reason = executor.detect_plan_deviation(slot, {"battery_soc": 34.0}, cfg)
    assert detected is True
    assert reason == "SOC_DEVIATION_BELOW_16.0_PCT_POINTS"


def test_soc_deviation_interpolates_current_slot():
    cfg = _cfg()
    tz = ZoneInfo(cfg.system.timezone)
    now = datetime(2026, 8, 10, 10, 7, 30, tzinfo=tz)
    slot = {
        "slot_start": datetime(2026, 8, 10, 10, 0, tzinfo=tz).isoformat(),
        "soc_start_pct": 40.0,
        "soc_end_pct": 60.0,
    }
    detected, reason = executor.detect_plan_deviation(slot, {"battery_soc": 65.0}, cfg, now=now)
    assert detected is True
    assert reason == "SOC_DEVIATION_ABOVE_15.0_PCT_POINTS"


def test_soc_deviation_alert_message_is_concise_and_directional():
    assert executor.soc_deviation_alert_message("SOC_DEVIATION_ABOVE_15.7_PCT_POINTS", 67) == (
        "FVE ALERT: významná odchylka: SOC je o 15.7 % nad plánem (aktuálně 67%)"
    )


def test_soc_alert_deduplicates_even_when_value_changes():
    cfg = _cfg({"system": {"dry_run": False, "battery_write_enabled": True}})
    now = datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    calls = []
    original = executor.alerting.notify.send
    executor.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
    tmp = tempfile.mkdtemp(prefix="planner_v11_soc_dedup_")
    kwargs = dict(
        cfg=cfg, forecast_valid=True, forecast_reasons=[], boiler_decision={},
        relay_health={"relay_status_ok": True}, detected_loads={},
        deviation_detected=True, alert_state_path=Path(tmp) / "alerts.json", actual_soc=67,
    )
    try:
        executor.send_executor_alerts(now=now, deviation_reason="SOC_DEVIATION_ABOVE_28.0_PCT_POINTS", **kwargs)
        executor.send_executor_alerts(now=now + timedelta(minutes=5), deviation_reason="SOC_DEVIATION_ABOVE_27.0_PCT_POINTS", **kwargs)
    finally:
        executor.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)
    assert len(calls) == 1


def test_completion_notifications_are_retry_safe_and_mark_persisted_state():
    cfg = _cfg()
    now = datetime(2026, 8, 10, 16, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    ledger = executor.boiler_state.empty_state()
    day = executor.boiler_state.today_entry(ledger, now.date())
    day.update({"full_detected_at": now.isoformat(), "estimated_delivered_kwh": 8.24})
    session = {"state": "CLOSED", "session_id": "ev-test", "delivered_kwh": 7.9}
    calls = []
    original = executor.alerting.notify.send
    executor.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
    tmp = tempfile.mkdtemp(prefix="planner_v11_completion_alerts_")
    try:
        outcomes = executor.send_executor_alerts(
            now=now, cfg=cfg, forecast_valid=True, forecast_reasons=[], boiler_decision={},
            relay_health={"relay_status_ok": True}, detected_loads={}, deviation_detected=False,
            deviation_reason="SOC_DEVIATION_OK_ABOVE_0.0_PCT_POINTS", boiler_ledger=ledger,
            ev_charging_session=session, alert_state_path=Path(tmp) / "alerts.json",
        )
    finally:
        executor.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)
    assert len(outcomes) == 2
    assert calls == [
        "Bojler je nahřátý naplno, dnes spotřeboval zhruba 8.2 kWh.",
        "Auto je nabité, spotřeba 7.9 kWh.",
    ]
    assert day["full_notification_sent_at"] == now.isoformat()
    assert session["completion_notification_sent_at"] == now.isoformat()
    assert executor.soc_deviation_alert_message("SOC_DEVIATION_BELOW_27.0_PCT_POINTS") == (
        "FVE ALERT: významná odchylka: SOC je o 27.0 % pod plánem"
    )


def test_send_executor_alerts_for_relay_health_failure():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    calls = []
    original = executor.alerting.notify.send
    executor.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
    tmp = tempfile.mkdtemp(prefix="planner_v10_test_executor_relay_health_")
    try:
        outcomes = executor.send_executor_alerts(
            now=now,
            cfg=cfg,
            forecast_valid=True,
            forecast_reasons=[],
            boiler_decision={"status": "blocked_by_dry_run_or_write_gate"},
            relay_health={"relay_status_ok": False},
            device_failures={"relay": {"consecutive_failures": 3}},
            detected_loads={"unexpected_load": {"active": False}},
            deviation_detected=False,
            deviation_reason="SOC_DEVIATION_OK_0.0_PCT_POINTS",
            alert_state_path=Path(tmp) / "alert_state.json",
        )
    finally:
        executor.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)

    assert len(outcomes) == 1
    assert "relé bojleru přes /98" in calls[0]


def test_eco_mapping_for_charge_discharge_hold_and_load_following_actions():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    charge = executor.eco_schedule_for_slot(1, now, {
        "battery_action": "FORCE_CHARGE", "battery_power_kw": 2.5, "soc_end_pct": 96.0,
    }, cfg)
    discharge = executor.eco_schedule_for_slot(2, now + timedelta(minutes=15), {
        "battery_action": "DISCHARGE_TO_GRID", "battery_power_kw": -9.0,
    }, cfg)
    hold = executor.eco_schedule_for_slot(3, now + timedelta(minutes=30), {
        "battery_action": "HOLD", "battery_power_kw": 0.0,
    }, cfg)
    self_use = executor.eco_schedule_for_slot(4, now + timedelta(minutes=45), {
        "battery_action": "SELF_USE", "battery_power_kw": -1.0,
    }, cfg)
    # max_soc_grid_pct is 95; production mapping deliberately reserves 1pp.
    assert (charge["enabled"], charge["power_pct"], charge["soc_pct"]) == (True, -25, 94)
    assert (discharge["enabled"], discharge["power_pct"]) == (True, 70)
    assert (hold["enabled"], hold["power_pct"], hold["soc_pct"]) == (True, -1, 100)
    assert self_use["enabled"] is False


def test_eco_schedule_truncates_midnight_crossing_slot():
    cfg = _cfg()
    now = datetime(2026, 8, 2, 23, 45, tzinfo=ZoneInfo(cfg.system.timezone))
    schedule = executor.eco_schedule_for_slot(1, now, {
        "battery_action": "HOLD", "battery_power_kw": 0.0,
    }, cfg)
    assert schedule["day_names"] == ["Sun"]
    assert (schedule["start_h"], schedule["start_m"]) == (23, 45)
    assert (schedule["end_h"], schedule["end_m"]) == (23, 59)
    assert schedule["enabled"] is True


def test_eco_quartet_merges_active_segments_and_uses_next_weekday_after_midnight():
    cfg = _cfg()
    now = datetime(2026, 8, 2, 23, 30, tzinfo=ZoneInfo(cfg.system.timezone))
    forecast = _forecast(cfg, now)
    start = executor.round_down_to_slot(now, 15)
    forecast["slots"] = [
        {"slot_start": (start + timedelta(minutes=15 * i)).isoformat(), "battery_action": "HOLD", "battery_power_kw": 0.0}
        for i in range(4)
    ]
    quartet = executor.build_eco_quartet(forecast, now, cfg)
    assert quartet[0]["day_names"] == ["Sun"]
    assert (quartet[0]["start_h"], quartet[0]["start_m"], quartet[0]["end_h"], quartet[0]["end_m"]) == (23, 30, 23, 59)
    assert quartet[1]["day_names"] == ["Mon"]
    assert (quartet[1]["start_h"], quartet[1]["start_m"], quartet[1]["end_h"], quartet[1]["end_m"]) == (0, 0, 0, 30)
    assert quartet[2]["enabled"] is False


def test_eco_quartet_skips_load_following_and_programs_upcoming_hold():
    cfg = _cfg()
    now = datetime(2026, 8, 3, 0, 40, tzinfo=ZoneInfo(cfg.system.timezone))
    start = executor.round_down_to_slot(now, 15)
    forecast = _forecast(cfg, now)
    actions = [
        ("DISCHARGE_TO_LOAD", -0.46),
        ("DISCHARGE_TO_LOAD", -0.46),
        ("DISCHARGE_TO_LOAD", -0.46),
        ("DISCHARGE_TO_LOAD", -0.24),
        ("DISCHARGE_TO_LOAD", -0.24),
        ("HOLD", 0.0),
        ("HOLD", 0.0),
        ("HOLD", 0.0),
    ]
    forecast["slots"] = [
        {"slot_start": (start + timedelta(minutes=15 * i)).isoformat(), "battery_action": action, "battery_power_kw": power}
        for i, (action, power) in enumerate(actions)
    ]
    quartet = executor.build_eco_quartet(forecast, now, cfg)
    assert quartet[0]["action"] == "HOLD"
    assert quartet[0]["enabled"] is True
    assert (quartet[0]["start_h"], quartet[0]["start_m"], quartet[0]["end_h"], quartet[0]["end_m"]) == (1, 45, 2, 30)
    assert all(item["enabled"] is False for item in quartet[1:])


def test_eco_quartet_retained_when_current_and_next_windows_match():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo(cfg.system.timezone))
    forecast = _forecast(cfg, now)
    # Give all four slots one concrete active segment.
    start = executor.round_down_to_slot(now, 15)
    forecast["slots"] = [
        {"slot_start": (start + timedelta(minutes=15 * i)).isoformat(), "battery_action": "HOLD", "battery_power_kw": 0.0}
        for i in range(4)
    ]
    first = executor.build_eco_quartet(forecast, now, cfg)
    previous = {"forecast_generated_at": forecast["generated_at"], "schedules": executor._eco_plan_fingerprint(first)}
    later = executor.build_eco_quartet(forecast, now + timedelta(minutes=15), cfg)
    needed, reason = executor.eco_plan_needs_write(previous, later, forecast)
    assert needed is False
    assert reason == "CURRENT_AND_NEXT_WINDOW_MATCH"


def test_failure_counter_alerts_only_after_third_consecutive_failure():
    now = datetime(2026, 7, 22, 15, 0, tzinfo=ZoneInfo("Europe/Prague"))
    tmp = Path(tempfile.mkdtemp(prefix="planner_v10_failure_counter_"))
    try:
        a = executor.update_device_failure_counter("relay", False, now=now, state_path=tmp / "devices.json")
        b = executor.update_device_failure_counter("relay", False, now=now, state_path=tmp / "devices.json")
        c = executor.update_device_failure_counter("relay", False, now=now, state_path=tmp / "devices.json")
        recovered = executor.update_device_failure_counter("relay", True, now=now, state_path=tmp / "devices.json")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert [a["consecutive_failures"], b["consecutive_failures"], c["consecutive_failures"]] == [1, 2, 3]
    assert recovered["consecutive_failures"] == 0
