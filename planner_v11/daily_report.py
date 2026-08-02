#!/usr/bin/env python3
"""
Daily report notifications for planner_v10.

VERSION = "1.2"

Changelog:
- v1.2 (2026-07-25): Treat GoodWe runtime power fields as watts, add forecast
  fixed-load and hard/opportunistic boiler split to the next-24h outlook.
- v1.1 (2026-07-24): Split output into two admin notifications: last 24 hours
  and next 24 hours. Add weather outlook, notify_admins delivery and daily
  deduplication.
- v1.0 (2026-07-22): Summarize previous-day runtime history and today's
  forecast. The script prints to stdout only; it does not send notifications.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from lib import alerting, notify
from lib.config import load_config


VERSION = "1.2"
PLANNER_DIR = Path(__file__).resolve().parent
STATE_DIR = PLANNER_DIR / "state"
DEFAULT_CONFIG_PATH = PLANNER_DIR / "config.toml"
FORECAST_PATH = STATE_DIR / "forecast_48h.json"
HISTORY_PATH = STATE_DIR / "state_history.jsonl"


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def iter_history(path: Path) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def parse_dt(value: Optional[str], tz: ZoneInfo) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def _kw_from_w(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return numeric / 1000.0


def summarize_history_window(rows: list[dict], start: datetime, end: datetime, tz: ZoneInfo, step_minutes: float) -> dict:
    slot_h = step_minutes / 60.0
    summary = {
        "samples": 0,
        "house_kwh": 0.0,
        "pv_kwh": 0.0,
        "grid_import_kwh": 0.0,
        "grid_export_kwh": 0.0,
        "ev_kwh": 0.0,
        "pool_kwh": 0.0,
        "boiler_kwh": 0.0,
        "unexpected_kwh": 0.0,
        "unexpected_events": 0,
        "data_quality_issues": 0,
    }
    for row in rows:
        ts = parse_dt(row.get("ts"), tz)
        if ts is None or ts < start or ts >= end:
            continue
        live = row.get("live_state", {})
        detected = row.get("detected_loads", {})
        summary["samples"] += 1
        summary["house_kwh"] += _kw_from_w(live.get("house_consumption")) * slot_h
        summary["pv_kwh"] += (_kw_from_w(live.get("ppv1")) + _kw_from_w(live.get("ppv2"))) * slot_h
        meter_kw = _kw_from_w(live.get("meter_active_power_total"))
        # GoodWe GW10K-ET: kladné = export, záporné = import (ověřeno
        # manuálními ECO testy 25.–29. 7. 2026; viz HANDOFF_v10.md).
        if meter_kw >= 0:
            summary["grid_export_kwh"] += meter_kw * slot_h
        else:
            summary["grid_import_kwh"] += abs(meter_kw) * slot_h
        summary["ev_kwh"] += float(detected.get("ev", {}).get("detected_kw", 0.0) or 0.0) * slot_h
        summary["pool_kwh"] += float(detected.get("pool", {}).get("detected_kw", 0.0) or 0.0) * slot_h
        summary["boiler_kwh"] += float(detected.get("boiler", {}).get("detected_kw", 0.0) or 0.0) * slot_h
        unexpected_kw = float(detected.get("unexpected_load", {}).get("kw", 0.0) or 0.0)
        summary["unexpected_kwh"] += unexpected_kw * slot_h
        if detected.get("unexpected_load", {}).get("active"):
            summary["unexpected_events"] += 1
        if detected.get("data_quality") not in (None, "OK"):
            summary["data_quality_issues"] += 1
    return summary


def summarize_history(rows: list[dict], target_day: date, tz: ZoneInfo, step_minutes: float) -> dict:
    start = datetime(target_day.year, target_day.month, target_day.day, tzinfo=tz)
    return summarize_history_window(rows, start, start + timedelta(days=1), tz, step_minutes)


def _slot_weather(slot: dict) -> tuple[Optional[float], Optional[float]]:
    evidence = slot.get("evidence", {}) if isinstance(slot.get("evidence"), dict) else {}
    sun = evidence.get("sun_pct", slot.get("sun_pct"))
    cloud = evidence.get("cloudcover_pct", slot.get("cloudcover_pct"))
    try:
        sun_f = None if sun is None else float(sun)
    except (TypeError, ValueError):
        sun_f = None
    try:
        cloud_f = None if cloud is None else float(cloud)
    except (TypeError, ValueError):
        cloud_f = None
    return sun_f, cloud_f


def summarize_forecast_window(forecast: dict, start: datetime, end: datetime, tz: ZoneInfo) -> dict:
    summary = {
        "pv_kwh": 0.0,
        "fixed_load_kwh": 0.0,
        "boiler_kwh": 0.0,
        "boiler_hard_kwh": 0.0,
        "boiler_opportunistic_kwh": 0.0,
        "ev_kwh": 0.0,
        "grid_import_kwh": 0.0,
        "grid_export_kwh": 0.0,
        "planner_duration_seconds": forecast.get("diagnostics", {}).get("planner_duration_seconds") if isinstance(forecast.get("diagnostics"), dict) else None,
        "price_min": None,
        "price_max": None,
        "estimated_price_slots": 0,
        "sun_values": [],
        "cloud_values": [],
        "active_requests": forecast.get("active_requests", []) if isinstance(forecast, dict) else [],
    }
    for slot in forecast.get("slots", []) if isinstance(forecast, dict) else []:
        slot_start = parse_dt(slot.get("slot_start"), tz)
        if slot_start is None or slot_start < start or slot_start >= end:
            continue
        summary["pv_kwh"] += float(slot.get("pv_estimate_kwh", 0.0) or 0.0)
        summary["fixed_load_kwh"] += float(slot.get("fixed_load_kwh", 0.0) or 0.0)
        summary["boiler_kwh"] += float(slot.get("boiler_power_kw", 0.0) or 0.0) * 0.25
        summary["boiler_hard_kwh"] += float(slot.get("boiler_hard_kwh", 0.0) or 0.0)
        summary["boiler_opportunistic_kwh"] += float(slot.get("boiler_opportunistic_kwh", 0.0) or 0.0)
        summary["ev_kwh"] += float(slot.get("ev_load_kwh", 0.0) or 0.0)
        summary["grid_import_kwh"] += float(slot.get("grid_import_kwh", 0.0) or 0.0)
        summary["grid_export_kwh"] += float(slot.get("grid_export_kwh", 0.0) or 0.0)
        try:
            price = float(slot.get("price_eur_mwh"))
        except (TypeError, ValueError):
            price = None
        if price is not None:
            summary["price_min"] = price if summary["price_min"] is None else min(summary["price_min"], price)
            summary["price_max"] = price if summary["price_max"] is None else max(summary["price_max"], price)
        if slot.get("price_source") != "actual":
            summary["estimated_price_slots"] += 1
        sun, cloud = _slot_weather(slot)
        if sun is not None:
            summary["sun_values"].append(sun)
        if cloud is not None:
            summary["cloud_values"].append(cloud)
    return summary


def summarize_forecast(forecast: dict, target_day: date, tz: ZoneInfo) -> dict:
    start = datetime(target_day.year, target_day.month, target_day.day, tzinfo=tz)
    return summarize_forecast_window(forecast, start, start + timedelta(days=1), tz)


def fmt(value: float, suffix: str = " kWh") -> str:
    return f"{value:.1f}{suffix}"


def fmt_price(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f} EUR/MWh"


def fmt_avg(values: list[float], suffix: str = " %") -> str:
    return "n/a" if not values else f"{sum(values) / len(values):.0f}{suffix}"


def fmt_seconds(value: Any) -> str:
    try:
        return f"{float(value):.1f} s"
    except (TypeError, ValueError):
        return "n/a"


def request_lines(active_requests: list) -> list[str]:
    if not isinstance(active_requests, list) or not active_requests:
        return ["aktivní požadavky: žádné"]
    lines = ["aktivní požadavky:"]
    for req in active_requests[:5]:
        if not isinstance(req, dict):
            continue
        rtype = req.get("type", "unknown")
        rec = req.get("recommendation", {}) if isinstance(req.get("recommendation"), dict) else {}
        if rtype == "ev_charge":
            lines.append(
                f"- auto {req.get('required_ac_kwh', 'n/a')} kWh do {req.get('deadline', 'n/a')}: "
                f"feasible={rec.get('feasible')}, start={rec.get('recommended_start') or 'zatím bez startu'}"
            )
        elif rtype == "boiler_full":
            lines.append(f"- bojler do {req.get('deadline', 'n/a')}")
        elif rtype == "additional_load":
            lines.append(f"- zátěž {req.get('power_kw', 'n/a')} kW")
        else:
            lines.append(f"- {rtype}")
    if len(active_requests) > 5:
        lines.append(f"... a dalších {len(active_requests) - 5}")
    return lines


def build_history_report(*, now: Optional[datetime] = None, target_end: Optional[datetime] = None) -> str:
    cfg = load_config(DEFAULT_CONFIG_PATH)
    tz = ZoneInfo(cfg.system.timezone)
    end = target_end or now or datetime.now(tz)
    end = end.replace(tzinfo=tz) if end.tzinfo is None else end.astimezone(tz)
    start = end - timedelta(hours=24)
    hist = summarize_history_window(iter_history(HISTORY_PATH), start, end, tz, cfg.system.execution_step_minutes)

    lines = [
        f"FVE report za posledních 24 hodin (daily_report.py v{VERSION})",
        f"období: {start.isoformat(timespec='minutes')} – {end.isoformat(timespec='minutes')}",
        f"vzorky executoru: {hist['samples']}",
        f"PV výroba:         {fmt(hist['pv_kwh'])}",
        f"spotřeba domu:     {fmt(hist['house_kwh'])}",
        f"import/export:     {fmt(hist['grid_import_kwh'])} / {fmt(hist['grid_export_kwh'])}",
        f"EV detekováno:     {fmt(hist['ev_kwh'])}",
        f"bazén detekováno:  {fmt(hist['pool_kwh'])}",
        f"bojler detekováno: {fmt(hist['boiler_kwh'])}",
        f"neočekávané:       {fmt(hist['unexpected_kwh'])}, aktivní vzorky: {hist['unexpected_events']}",
        f"kvalita dat:       {'OK' if hist['data_quality_issues'] == 0 else str(hist['data_quality_issues']) + ' problémových vzorků'}",
    ]
    return "\n".join(lines)


def build_outlook_report(*, now: Optional[datetime] = None) -> str:
    cfg = load_config(DEFAULT_CONFIG_PATH)
    tz = ZoneInfo(cfg.system.timezone)
    start = now or datetime.now(tz)
    start = start.replace(tzinfo=tz) if start.tzinfo is None else start.astimezone(tz)
    end = start + timedelta(hours=24)
    forecast_doc = read_json(FORECAST_PATH, {})
    forecast = summarize_forecast_window(forecast_doc, start, end, tz)
    lines = [
        f"FVE výhled na dalších 24 hodin (daily_report.py v{VERSION})",
        f"období: {start.isoformat(timespec='minutes')} – {end.isoformat(timespec='minutes')}",
        f"PV odhad:          {fmt(forecast['pv_kwh'])}",
        f"počasí:            slunce průměr {fmt_avg(forecast['sun_values'])}, oblačnost průměr {fmt_avg(forecast['cloud_values'])}",
        f"ceny min/max:      {fmt_price(forecast['price_min'])} / {fmt_price(forecast['price_max'])}",
        f"estimated ceny:    {forecast['estimated_price_slots']} slotů",
        f"fixed load plán:   {fmt(forecast['fixed_load_kwh'])}",
        f"bojler plán:       {fmt(forecast['boiler_kwh'])} (hard {fmt(forecast['boiler_hard_kwh'])}, oportun. {fmt(forecast['boiler_opportunistic_kwh'])})",
        f"EV plán:           {fmt(forecast['ev_kwh'])}",
        f"grid import/export:{fmt(forecast['grid_import_kwh'])} / {fmt(forecast['grid_export_kwh'])}",
        f"planner duration:  {fmt_seconds(forecast['planner_duration_seconds'])}",
        *request_lines(forecast["active_requests"]),
    ]
    return "\n".join(lines)


def build_report(target_day: Optional[date] = None) -> str:
    cfg = load_config(DEFAULT_CONFIG_PATH)
    tz = ZoneInfo(cfg.system.timezone)
    if target_day is not None:
        end = datetime(target_day.year, target_day.month, target_day.day, tzinfo=tz) + timedelta(days=1)
        return build_history_report(target_end=end) + "\n\n" + build_outlook_report(now=end)
    now = datetime.now(tz)
    return build_history_report(now=now) + "\n\n" + build_outlook_report(now=now)


def send_reports(*, now: datetime, notify_script: Path, alert_state: Path, force: bool = False) -> list[dict]:
    day_key = now.date().isoformat()
    return [
        alerting.notify_daily(
            f"daily_report.history.{day_key}",
            build_history_report(now=now),
            state_path=alert_state,
            notify_script=notify_script,
            now=now,
            force=force,
        ),
        alerting.notify_daily(
            f"daily_report.outlook.{day_key}",
            build_outlook_report(now=now),
            state_path=alert_state,
            notify_script=notify_script,
            now=now,
            force=force,
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="planner_v10 daily report")
    parser.add_argument("--date", help="Compatibility/debug: summarize 24h ending after YYYY-MM-DD")
    parser.add_argument("--send", action="store_true", help="Send both reports via notify_admins.sh")
    parser.add_argument("--quiet", action="store_true", help="Do not print report text in --send mode")
    parser.add_argument("--force", action="store_true", help="Bypass daily report deduplication")
    parser.add_argument("--notify-script", type=Path, default=notify.DEFAULT_NOTIFY_SCRIPT)
    parser.add_argument("--alert-state", type=Path, default=alerting.DEFAULT_ALERT_STATE_PATH)
    args = parser.parse_args()
    cfg = load_config(DEFAULT_CONFIG_PATH)
    tz = ZoneInfo(cfg.system.timezone)
    if args.date:
        d = date.fromisoformat(args.date)
        now = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(days=1)
    else:
        now = datetime.now(tz)

    if args.send:
        outcomes = send_reports(now=now, notify_script=args.notify_script, alert_state=args.alert_state, force=args.force)
        if not args.quiet:
            print(build_history_report(now=now))
            print()
            print(build_outlook_report(now=now))
            print()
            print("notify outcomes:", outcomes)
        return 0 if all(o.get("sent") or o.get("reason") == "deduplicated" for o in outcomes) else 1

    print(build_history_report(now=now))
    print()
    print(build_outlook_report(now=now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())