"""Hermetické testy pro planner.py v10 orchestration helpers.

Nečtou GoodWe ani produkční data; ověřují formát slotů, fallbacky,
terminální hodnotu a minimální forecast JSON kontrakt.
"""
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.config import parse_config_dict  # noqa: E402
from lib import optimizer  # noqa: E402

PLANNER_PATH = Path(__file__).resolve().parent.parent / "planner.py"
spec = importlib.util.spec_from_file_location("planner_v10_module", PLANNER_PATH)
planner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = planner
spec.loader.exec_module(planner)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.toml")


def _cfg(overrides: dict | None = None):
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    raw = copy.deepcopy(raw)
    raw["system"]["horizon_hours"] = 1
    if overrides:
        for section, kv in overrides.items():
            raw.setdefault(section, {}).update(kv)
    return parse_config_dict(raw)


def _make_price_day(path: str, price: float) -> None:
    lines = []
    for h in range(24):
        for m in (0, 15, 30, 45):
            lines.append(f"{h:02d}:{m:02d};{price:.2f}".replace(".", ","))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _make_weather_day(path: str) -> None:
    lines = ["datetime;sun_pct;cloudcover_pct;shortwave_wm2;pv_estimate_kwh"]
    day = Path(path).stem
    for h in range(24):
        lines.append(f"{day}T{h:02d}:00;50;25;100;4.0")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def test_round_down_to_slot():
    dt = datetime(2026, 7, 22, 14, 44, 59)
    assert planner.round_down_to_slot(dt, 15) == datetime(2026, 7, 22, 14, 30)


def test_build_plan_inputs_uses_actual_price_weather_and_pool_load():
    cfg = _cfg()
    tmp = tempfile.mkdtemp(prefix="planner_v10_test_planner_")
    try:
        prices_dir = os.path.join(tmp, "prices")
        weather_dir = os.path.join(tmp, "weather")
        os.makedirs(prices_dir)
        os.makedirs(weather_dir)
        day = "2026-07-22"
        _make_price_day(os.path.join(prices_dir, f"{day}.csv"), 100.0)
        _make_weather_day(os.path.join(weather_dir, f"{day}.csv"))

        starts = [datetime(2026, 7, 22, 9, 30) + timedelta(minutes=15 * i) for i in range(4)]
        opt_slots, meta = planner.build_plan_inputs(
            starts, cfg, prices_dir=prices_dir, weather_dir=weather_dir, base_profile={}
        )
        assert len(opt_slots) == 4
        assert meta[0].price_source == "actual"
        assert abs(meta[0].pv_estimate_kwh - 1.0) < 1e-9  # 4 kWh/hour / 4
        assert meta[0].base_load_source == "fallback_overnight_reserve"
        assert meta[0].pool_load_kwh > 0.0  # 09:30 je v ranním okně bazénu
        assert abs(opt_slots[0].fixed_load_kwh - meta[0].fixed_load_kwh) < 1e-9
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_terminal_value_is_capped_by_last_slot_import():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 12, 0)
    meta = [
        planner.SlotPlanInput(
            slot_start=now + timedelta(minutes=15 * i),
            price_eur_mwh=300.0,
            price_export_eur_mwh=300.0,
            price_source="actual",
            import_price_czk_kwh=high,
            export_revenue_czk_kwh=0.0,
            pv_estimate_kwh=0.0,
            base_load_expected_kwh=0.0,
            base_load_reserve_kwh=0.0,
            base_load_source="test",
            pool_load_kwh=0.0,
            pool_heat_pump_kwh=0.0,
            additional_load_kwh=0.0,
            export_allowed=True,
            effective_import_nonpositive=False,
            sun_pct=None,
            cloudcover_pct=None,
        )
        for i, high in enumerate([10.0, 10.0, 10.0, 1.0])
    ]
    assert planner.compute_terminal_value_czk_per_kwh(meta, cfg) <= 1.0


def test_build_forecast_document_minimal_contract():
    cfg = _cfg()
    start = datetime(2026, 7, 22, 12, 0)
    meta = [
        planner.SlotPlanInput(
            slot_start=start,
            price_eur_mwh=100.0,
            price_export_eur_mwh=100.0,
            price_source="actual",
            import_price_czk_kwh=1.0,
            export_revenue_czk_kwh=0.5,
            pv_estimate_kwh=0.0,
            base_load_expected_kwh=0.1,
            base_load_reserve_kwh=0.2,
            base_load_source="profile",
            pool_load_kwh=0.0,
            pool_heat_pump_kwh=0.0,
            additional_load_kwh=0.0,
            export_allowed=True,
            effective_import_nonpositive=False,
            sun_pct=None,
            cloudcover_pct=None,
        )
    ]
    result = optimizer.OptimizerResult(
        status="optimal",
        slots=[
            optimizer.SlotResult(
                slot_start=start,
                pv_to_fixed_load_kwh=0.0,
                pv_to_boiler_kwh=0.0,
                pv_to_battery_kwh=0.0,
                pv_to_grid_kwh=0.0,
                pv_curtailed_kwh=0.0,
                grid_to_fixed_load_kwh=0.1,
                grid_to_boiler_kwh=0.0,
                grid_to_battery_kwh=0.0,
                battery_to_fixed_load_kwh=0.0,
                battery_to_boiler_kwh=0.0,
                battery_to_grid_kwh=0.0,
                soc_start_kwh=7.4,
                soc_end_kwh=7.4,
                boiler_phase_on=(False, False, False),
                ev_delivered_kwh=0.0,
                grid_import_kwh=0.1,
                grid_export_kwh=0.0,
                battery_action="HOLD",
                boiler_hard_kwh=0.0,
                boiler_opportunistic_kwh=0.0,
            )
        ],
        economic_objective_czk=0.1,
        terminal_soc_kwh=7.4,
    )
    doc = planner.build_forecast_document(
        generated_at=start,
        valid_until=start + timedelta(minutes=90),
        cfg=cfg,
        live_state={"battery_soc": 50.0},
        meta=meta,
        result=result,
        active_requests=[],
        terminal_value_czk_per_kwh=0.0,
        planner_duration_seconds=1.234,
    )
    assert doc["schema_version"] == 10
    assert doc["solver"]["status"] == "optimal"
    assert doc["slots"][0]["slot_start"] == start.isoformat()
    assert "soc_start_pct" in doc["slots"][0]
    assert "reason_codes" in doc["slots"][0]
    assert doc["slots"][0]["fixed_load_kwh"] == 0.1
    assert doc["diagnostics"]["planned_boiler_kwh_next_24h"] == 0.0
    assert doc["diagnostics"]["planned_boiler_hard_kwh_next_24h"] == 0.0
    assert doc["diagnostics"]["planned_boiler_opportunistic_kwh_next_24h"] == 0.0
    assert doc["diagnostics"]["planner_duration_seconds"] == 1.234


def test_forecast_document_splits_boiler_hard_and_opportunistic_energy():
    cfg = _cfg()
    start = datetime(2026, 7, 22, 12, 0)
    meta = [
        planner.SlotPlanInput(
            slot_start=start,
            price_eur_mwh=-50.0,
            price_export_eur_mwh=-50.0,
            price_source="actual",
            import_price_czk_kwh=-0.1,
            export_revenue_czk_kwh=-1.0,
            pv_estimate_kwh=0.0,
            base_load_expected_kwh=0.0,
            base_load_reserve_kwh=0.0,
            base_load_source="profile",
            pool_load_kwh=0.0,
            pool_heat_pump_kwh=0.0,
            additional_load_kwh=0.0,
            export_allowed=False,
            effective_import_nonpositive=True,
            sun_pct=None,
            cloudcover_pct=None,
        )
    ]
    result = optimizer.OptimizerResult(
        status="optimal",
        slots=[
            optimizer.SlotResult(
                slot_start=start,
                pv_to_fixed_load_kwh=0.0,
                pv_to_boiler_kwh=0.0,
                pv_to_battery_kwh=0.0,
                pv_to_grid_kwh=0.0,
                pv_curtailed_kwh=0.0,
                grid_to_fixed_load_kwh=0.0,
                grid_to_boiler_kwh=1.0,
                grid_to_battery_kwh=0.0,
                battery_to_fixed_load_kwh=0.0,
                battery_to_boiler_kwh=0.0,
                battery_to_grid_kwh=0.0,
                soc_start_kwh=7.4,
                soc_end_kwh=7.4,
                boiler_phase_on=(True, True, False),
                ev_delivered_kwh=0.0,
                grid_import_kwh=1.0,
                grid_export_kwh=0.0,
                battery_action="HOLD",
                boiler_hard_kwh=0.5,
                boiler_opportunistic_kwh=0.5,
            )
        ],
        economic_objective_czk=-1.0,
        terminal_soc_kwh=7.4,
    )

    doc = planner.build_forecast_document(
        generated_at=start,
        valid_until=start + timedelta(minutes=90),
        cfg=cfg,
        live_state={"battery_soc": 50.0},
        meta=meta,
        result=result,
        active_requests=[],
        terminal_value_czk_per_kwh=0.0,
    )

    slot = doc["slots"][0]
    assert slot["boiler_hard_kwh"] == 0.5
    assert slot["boiler_opportunistic_kwh"] == 0.5
    assert "BOILER_HARD_REQUEST" in slot["reason_codes"]
    assert "BOILER_OPPORTUNISTIC_ECONOMIC" in slot["reason_codes"]
    assert doc["diagnostics"]["planned_boiler_hard_kwh_next_24h"] == 0.5
    assert doc["diagnostics"]["planned_boiler_opportunistic_kwh_next_24h"] == 0.5


def test_additional_load_includes_unannounced_ev_assumption():
    cfg = _cfg()
    now = datetime(2026, 7, 22, 12, 0)
    detected = {
        "unannounced_ev_load": {
            "active": True,
            "power_kw": 1.6,
            "valid_until": (now + timedelta(hours=5)).isoformat(),
        }
    }
    kwh = planner.additional_load_kwh_for_slot(now, cfg.system.planning_step_minutes, [], detected)
    assert abs(kwh - 0.4) < 1e-9
    breakdown = planner.additional_load_breakdown_for_slot(now, cfg.system.planning_step_minutes, [], detected)
    assert breakdown["unannounced_ev_kw"] == 1.6
    assert breakdown["unannounced_ev_kwh"] == 0.4


def test_boiler_daily_budget_uses_delivered_today_and_full_future_limit():
    cfg = _cfg({"system": {"horizon_hours": 2}})
    now = datetime(2026, 8, 2, 23, 30)
    starts = [now + timedelta(minutes=15 * idx) for idx in range(8)]
    ledger = {"days": {"2026-08-02": {"commanded_kwh": 9.0, "estimated_delivered_kwh": 4.5}}}
    limits, diagnostics = planner.boiler_daily_budget(starts, cfg, now, ledger)
    assert limits[now.date()] == 10.5
    assert limits[(now + timedelta(days=1)).date()] == 15.0
    assert diagnostics["2026-08-02"]["commanded_before_plan_kwh"] == 9.0
    assert diagnostics["2026-08-02"]["estimated_delivered_before_plan_kwh"] == 4.5


def test_choose_requests_defers_far_ev_deadline_without_pv_rich_candidate():
    cfg = _cfg({"system": {"horizon_hours": 2}})
    start = datetime(2026, 7, 24, 12, 0)
    starts = [start + timedelta(minutes=15 * i) for i in range(8)]
    opt_slots = [
        optimizer.SlotInput(
            slot_start=slot_start,
            price_import_czk_kwh=1.0,
            price_export_czk_kwh=0.0,
            export_allowed=True,
            effective_import_nonpositive=False,
            pv_kwh=0.0,
            fixed_load_kwh=0.1,
        )
        for slot_start in starts
    ]
    requests = [
        {
            "id": "ev-far",
            "type": "ev_charge",
            "status": "active",
            "available_from": start,
            "deadline": start + timedelta(days=5),
            "required_ac_kwh": 2.0,
        }
    ]

    ev_req, boiler_req, summary = planner.choose_requests(
        requests,
        starts,
        cfg,
        opt_slots,
        initial_soc_kwh=7.4,
        terminal_value_czk_per_kwh=0.0,
    )

    assert ev_req is None
    assert boiler_req is None
    assert summary[0]["deadline_outside_current_horizon"] is True
    assert summary[0]["recommendation"]["recommended_start"] is None
    assert "není PV-rich" in summary[0]["recommendation"]["reason"]


def test_send_planner_alerts_for_infeasible_ev_request():
    cfg = _cfg()
    now = datetime(2026, 7, 24, 12, 0)
    result = optimizer.OptimizerResult(status="optimal", ev_unserved_kwh=0.0)
    active_requests = [
        {
            "type": "ev_charge",
            "id": "ev-impossible",
            "required_ac_kwh": 20.0,
            "deadline": "2026-07-24T14:00:00+02:00",
            "deadline_outside_current_horizon": False,
            "recommendation": {"feasible": False, "reason": "deadline příliš blízko"},
        }
    ]
    calls = []
    original = planner.alerting.notify.send
    planner.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
    tmp = tempfile.mkdtemp(prefix="planner_v10_test_planner_alerts_")
    try:
        outcomes = planner.send_planner_alerts(
            now=now,
            cfg=cfg,
            result=result,
            active_requests=active_requests,
            requests_path=Path(tmp) / "requests.json",
            alert_state_path=Path(tmp) / "alert_state.json",
        )
    finally:
        planner.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)

    assert len(outcomes) == 1
    assert outcomes[0]["sent"] is True
    assert "požadavek na nabití auta" in calls[0]


def test_send_planner_alerts_does_not_alert_deferred_far_horizon_ev():
    cfg = _cfg()
    now = datetime(2026, 7, 24, 12, 0)
    result = optimizer.OptimizerResult(status="optimal")
    active_requests = [
        {
            "type": "ev_charge",
            "id": "ev-far",
            "deadline_outside_current_horizon": True,
            "recommendation": {
                "feasible": False,
                "reason": "Deadline je mimo aktuální 48h horizont a nejpozdější bezpečný start je také mimo horizont.",
            },
        }
    ]
    calls = []
    original = planner.alerting.notify.send
    planner.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
    tmp = tempfile.mkdtemp(prefix="planner_v10_test_planner_alerts_")
    try:
        outcomes = planner.send_planner_alerts(
            now=now,
            cfg=cfg,
            result=result,
            active_requests=active_requests,
            requests_path=Path(tmp) / "requests.json",
            alert_state_path=Path(tmp) / "alert_state.json",
        )
    finally:
        planner.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)

    assert outcomes == []
    assert calls == []


def _write_ev_request_with_baseline(path: Path, *, status: str = "active", baseline: str | None = "2026-08-05T11:00:00+02:00"):
    item = {
        "id": "ev-1",
        "request_id": "ev-1",
        "type": "ev_charge",
        "status": status,
    }
    if baseline is not None:
        item["ev_schedule_notification"] = {
            "initial_notified_start": "2026-08-05T11:00:00+02:00",
            "initial_notified_end": "2026-08-05T14:00:00+02:00",
            "last_notified_start": baseline,
            "last_notified_end": "2026-08-05T14:00:00+02:00",
            "last_notified_at": "2026-08-04T00:00:00+02:00",
        }
    path.write_text(json.dumps({"schema_version": 10, "requests": [item]}), encoding="utf-8")


def _ev_summary(
    start: str | None,
    end: str | None = "2026-08-05T14:00:00+02:00",
    *,
    feasible: bool = True,
    request_id: str = "ev-1",
):
    return [{
        "id": request_id,
        "type": "ev_charge",
        "recommendation": {
            "feasible": feasible,
            "recommended_start": start,
            "expected_end": end,
        },
    }]


def test_ev_schedule_change_ignores_59_minutes_and_alerts_exactly_60_both_directions():
    cfg = _cfg()
    now = datetime.fromisoformat("2026-08-04T00:30:00+02:00")
    tmp = tempfile.mkdtemp(prefix="planner_v10_test_ev_shift_")
    original = planner.alerting.notify.send
    calls = []
    planner.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
    try:
        requests_path = Path(tmp) / "requests.json"
        alert_path = Path(tmp) / "alert_state.json"
        _write_ev_request_with_baseline(requests_path)
        ignored = planner.send_ev_schedule_change_alerts(
            now=now,
            cfg=cfg,
            active_requests=_ev_summary("2026-08-05T10:01:00+02:00"),
            requests_path=requests_path,
            alert_state_path=alert_path,
        )
        earlier = planner.send_ev_schedule_change_alerts(
            now=now,
            cfg=cfg,
            active_requests=_ev_summary("2026-08-05T10:00:00+02:00", "2026-08-05T13:00:00+02:00"),
            requests_path=requests_path,
            alert_state_path=alert_path,
        )
        later = planner.send_ev_schedule_change_alerts(
            now=now + timedelta(minutes=1),
            cfg=cfg,
            active_requests=_ev_summary("2026-08-05T11:00:00+02:00", "2026-08-05T14:00:00+02:00"),
            requests_path=requests_path,
            alert_state_path=alert_path,
        )
        metadata = json.loads(requests_path.read_text(encoding="utf-8"))["requests"][0]["ev_schedule_notification"]
        assert ignored == []
        assert earlier[0]["sent"] is True and earlier[0]["shift_minutes"] == 60.0
        assert later[0]["sent"] is True and later[0]["shift_minutes"] == 60.0
        assert "10:00–13:00" in calls[0] and "dříve" in calls[0]
        assert "11:00–14:00" in calls[1] and "později" in calls[1]
        assert metadata["initial_notified_start"] == "2026-08-05T11:00:00+02:00"
        assert metadata["last_notified_start"] == "2026-08-05T11:00:00+02:00"
    finally:
        planner.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_ev_schedule_change_accumulates_small_shifts_against_last_announced_start():
    cfg = _cfg()
    now = datetime.fromisoformat("2026-08-04T00:30:00+02:00")
    tmp = tempfile.mkdtemp(prefix="planner_v10_test_ev_shift_")
    original = planner.alerting.notify.send
    calls = []
    planner.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
    try:
        requests_path = Path(tmp) / "requests.json"
        alert_path = Path(tmp) / "alert_state.json"
        _write_ev_request_with_baseline(requests_path)
        first = planner.send_ev_schedule_change_alerts(
            now=now,
            cfg=cfg,
            active_requests=_ev_summary("2026-08-05T10:45:00+02:00"),
            requests_path=requests_path,
            alert_state_path=alert_path,
        )
        second = planner.send_ev_schedule_change_alerts(
            now=now + timedelta(minutes=1),
            cfg=cfg,
            active_requests=_ev_summary("2026-08-05T10:00:00+02:00", "2026-08-05T13:00:00+02:00"),
            requests_path=requests_path,
            alert_state_path=alert_path,
        )
        metadata = json.loads(requests_path.read_text(encoding="utf-8"))["requests"][0]["ev_schedule_notification"]
        assert first == []
        assert second[0]["sent"] is True
        assert len(calls) == 1
        assert metadata["last_notified_start"] == "2026-08-05T10:00:00+02:00"
    finally:
        planner.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_ev_schedule_change_retains_baseline_on_send_failure_and_retries():
    cfg = _cfg()
    now = datetime.fromisoformat("2026-08-04T00:30:00+02:00")
    tmp = tempfile.mkdtemp(prefix="planner_v10_test_ev_shift_")
    original = planner.alerting.notify.send
    send_results = iter((False, True))
    planner.alerting.notify.send = lambda message, **kwargs: next(send_results)
    try:
        requests_path = Path(tmp) / "requests.json"
        alert_path = Path(tmp) / "alert_state.json"
        _write_ev_request_with_baseline(requests_path)
        failed = planner.send_ev_schedule_change_alerts(
            now=now,
            cfg=cfg,
            active_requests=_ev_summary("2026-08-05T10:00:00+02:00"),
            requests_path=requests_path,
            alert_state_path=alert_path,
        )
        after_failure = json.loads(requests_path.read_text(encoding="utf-8"))["requests"][0]["ev_schedule_notification"]
        retried = planner.send_ev_schedule_change_alerts(
            now=now + timedelta(minutes=1),
            cfg=cfg,
            active_requests=_ev_summary("2026-08-05T10:00:00+02:00"),
            requests_path=requests_path,
            alert_state_path=alert_path,
        )
        after_retry = json.loads(requests_path.read_text(encoding="utf-8"))["requests"][0]["ev_schedule_notification"]
        assert failed[0]["reason"] == "send_failed"
        assert after_failure["last_notified_start"] == "2026-08-05T11:00:00+02:00"
        assert retried[0]["sent"] is True
        assert after_retry["last_notified_start"] == "2026-08-05T10:00:00+02:00"
    finally:
        planner.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_ev_schedule_change_skips_missing_baseline_start_infeasible_and_inactive_request():
    cfg = _cfg()
    now = datetime.fromisoformat("2026-08-04T00:30:00+02:00")
    tmp = tempfile.mkdtemp(prefix="planner_v10_test_ev_shift_")
    original = planner.alerting.notify.send
    calls = []
    planner.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
    try:
        requests_path = Path(tmp) / "requests.json"
        alert_path = Path(tmp) / "alert_state.json"
        cases = [
            ("active", None, _ev_summary("2026-08-05T10:00:00+02:00")),
            ("active", "2026-08-05T11:00:00+02:00", _ev_summary(None)),
            ("active", "2026-08-05T11:00:00+02:00", _ev_summary("2026-08-05T10:00:00+02:00", feasible=False)),
            ("active", "2026-08-05T11:00:00+02:00", _ev_summary("2026-08-05T10:00:00+02:00", request_id="ev-other")),
            ("replaced", "2026-08-05T11:00:00+02:00", _ev_summary("2026-08-05T10:00:00+02:00")),
        ]
        for status, baseline, summary in cases:
            _write_ev_request_with_baseline(requests_path, status=status, baseline=baseline)
            assert planner.send_ev_schedule_change_alerts(
                now=now,
                cfg=cfg,
                active_requests=summary,
                requests_path=requests_path,
                alert_state_path=alert_path,
            ) == []
        assert calls == []
    finally:
        planner.alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)