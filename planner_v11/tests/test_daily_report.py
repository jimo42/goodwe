"""Hermetic tests for daily_report.py notification formatting."""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPORT_PATH = Path(__file__).resolve().parent.parent / "daily_report.py"
spec = importlib.util.spec_from_file_location("daily_report_v10_module", REPORT_PATH)
daily_report = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = daily_report
spec.loader.exec_module(daily_report)


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="planner_v10_test_daily_report_")


def test_build_report_contains_history_and_weather_outlook():
    tmp = _tmpdir()
    original_history = daily_report.HISTORY_PATH
    original_forecast = daily_report.FORECAST_PATH
    try:
        root = Path(tmp)
        history = root / "state_history.jsonl"
        forecast = root / "forecast_48h.json"
        tz = ZoneInfo("Europe/Prague")
        now = datetime(2026, 7, 24, 7, 0, tzinfo=tz)
        history.write_text(
            json.dumps({
                "ts": (now - timedelta(hours=1)).isoformat(),
                "live_state": {
                    "house_consumption": 1000,
                    "ppv1": 1200,
                    "ppv2": 800,
                    "meter_active_power_total": -500,
                },
                "detected_loads": {
                    "data_quality": "OK",
                    "ev": {"detected_kw": 0.0},
                    "pool": {"detected_kw": 0.0},
                    "boiler": {"detected_kw": 0.0},
                    "unexpected_load": {"kw": 0.0, "active": False},
                },
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        forecast.write_text(
            json.dumps({
                "active_requests": [],
                "slots": [
                    {
                        "slot_start": (now + timedelta(hours=1)).isoformat(),
                        "pv_estimate_kwh": 1.0,
                        "fixed_load_kwh": 0.5,
                        "boiler_power_kw": 2.0,
                        "boiler_hard_kwh": 0.25,
                        "boiler_opportunistic_kwh": 0.25,
                        "ev_load_kwh": 0.0,
                        "grid_import_kwh": 0.2,
                        "grid_export_kwh": 0.1,
                        "price_eur_mwh": 50.0,
                        "price_source": "actual",
                        "evidence": {"sun_pct": 60, "cloudcover_pct": 20},
                    }
                ],
                "diagnostics": {"planner_duration_seconds": 12.3},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        daily_report.HISTORY_PATH = history
        daily_report.FORECAST_PATH = forecast

        report = daily_report.build_history_report(now=now) + "\n\n" + daily_report.build_outlook_report(now=now)

        assert "FVE report za posledních 24 hodin" in report
        assert "FVE výhled na dalších 24 hodin" in report
        assert "počasí:" in report
        assert "slunce průměr" in report
        assert "fixed load plán:" in report
        assert "hard 0.2 kWh, oportun. 0.2 kWh" in report
        assert "planner duration:  12.3 s" in report
    finally:
        daily_report.HISTORY_PATH = original_history
        daily_report.FORECAST_PATH = original_forecast
        shutil.rmtree(tmp, ignore_errors=True)


def test_history_interprets_positive_meter_as_export_and_negative_as_import():
    tz = ZoneInfo("Europe/Prague")
    start = datetime(2026, 7, 24, tzinfo=tz)
    rows = [
        {"ts": start.isoformat(), "live_state": {"meter_active_power_total": 500}, "detected_loads": {}},
        {"ts": (start + timedelta(minutes=5)).isoformat(), "live_state": {"meter_active_power_total": -300}, "detected_loads": {}},
    ]

    summary = daily_report.summarize_history_window(rows, start, start + timedelta(minutes=10), tz, step_minutes=5)

    assert abs(summary["grid_export_kwh"] - (0.5 * 5 / 60)) < 1e-9
    assert abs(summary["grid_import_kwh"] - (0.3 * 5 / 60)) < 1e-9


def test_send_reports_sends_two_daily_messages_with_dedup():
    tmp = _tmpdir()
    original_send = daily_report.alerting.notify.send
    original_history = daily_report.HISTORY_PATH
    original_forecast = daily_report.FORECAST_PATH
    calls = []
    try:
        root = Path(tmp)
        history = root / "state_history.jsonl"
        forecast = root / "forecast_48h.json"
        history.write_text("", encoding="utf-8")
        forecast.write_text(json.dumps({"active_requests": [], "slots": []}), encoding="utf-8")
        daily_report.HISTORY_PATH = history
        daily_report.FORECAST_PATH = forecast
        daily_report.alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
        now = datetime(2026, 7, 24, 7, 0, tzinfo=ZoneInfo("Europe/Prague"))
        state = root / "alert_state.json"

        first = daily_report.send_reports(now=now, notify_script=root / "notify.sh", alert_state=state)
        second = daily_report.send_reports(now=now, notify_script=root / "notify.sh", alert_state=state)

        assert len(first) == 2
        assert all(item["sent"] for item in first)
        assert all(item["reason"] == "deduplicated" for item in second)
        assert len(calls) == 2
    finally:
        daily_report.alerting.notify.send = original_send
        daily_report.HISTORY_PATH = original_history
        daily_report.FORECAST_PATH = original_forecast
        shutil.rmtree(tmp, ignore_errors=True)