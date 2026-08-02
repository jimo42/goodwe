#!/usr/bin/env python3
"""
Human-readable planner_v11 status report.

VERSION = "1.2"

Changelog:
- v1.2 (2026-08-02): Add planner daily budget, executor economics, minute
  telemetry, phase-mask/headroom and commanded-vs-delivered ledger details.
- v1.1 (2026-07-27): Show the instantaneous signed smart-meter grid flow
  explicitly as import or export, including per-phase values when available.
- v1.0 (2026-07-22): Read-only status over forecast_48h.json,
  runtime_state.json and detected_loads.json.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from lib.config import load_config


VERSION = "1.2"
PLANNER_DIR = Path(__file__).resolve().parent
STATE_DIR = PLANNER_DIR / "state"
DEFAULT_CONFIG_PATH = PLANNER_DIR / "config.toml"
FORECAST_PATH = STATE_DIR / "forecast_48h.json"
RUNTIME_PATH = STATE_DIR / "runtime_state.json"
DETECTED_LOADS_PATH = STATE_DIR / "detected_loads.json"
BOILER_CONTROL_STATE_PATH = STATE_DIR / "boiler_control_state.json"


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def parse_dt(value: Optional[str], tz: ZoneInfo) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def find_current_slot(slots: list[dict], now: datetime, slot_minutes: int, tz: ZoneInfo) -> Optional[dict]:
    rounded = now.replace(minute=(now.minute // slot_minutes) * slot_minutes, second=0, microsecond=0)
    for slot in slots:
        slot_start = parse_dt(slot.get("slot_start"), tz)
        if slot_start == rounded:
            return slot
    return None


def fmt(value: Any, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def fmt_grid_flow(power_w: Any) -> str:
    """Format signed GoodWe smart-meter power (positive export, negative import)."""
    try:
        watts = float(power_w)
    except (TypeError, ValueError):
        return "n/a"
    if watts > 0:
        return f"export {watts:.0f} W"
    if watts < 0:
        return f"import {abs(watts):.0f} W"
    return "0 W (bez toku)"


def action_changes(slots: list[dict], now: datetime, hours: float, tz: ZoneInfo) -> list[dict]:
    end = now + timedelta(hours=hours)
    out: list[dict] = []
    last_key = None
    for slot in slots:
        slot_start = parse_dt(slot.get("slot_start"), tz)
        if slot_start is None or slot_start < now or slot_start > end:
            continue
        key = (slot.get("battery_action"), slot.get("boiler_power_kw"), slot.get("ev_load_kwh"))
        if key != last_key:
            out.append(slot)
            last_key = key
    return out


def build_status(*, hours: float, full: bool) -> str:
    cfg = load_config(DEFAULT_CONFIG_PATH)
    tz = ZoneInfo(cfg.system.timezone)
    now = datetime.now(tz)
    forecast = read_json(FORECAST_PATH, {})
    runtime = read_json(RUNTIME_PATH, {})
    detected = read_json(DETECTED_LOADS_PATH, {})
    ledger = read_json(BOILER_CONTROL_STATE_PATH, {})
    slots = forecast.get("slots", []) if isinstance(forecast, dict) else []
    current = find_current_slot(slots, now, cfg.system.planning_step_minutes, tz) if isinstance(slots, list) else None
    live = runtime.get("live_state", {}) if isinstance(runtime, dict) else {}
    boiler_decision = runtime.get("boiler_decision", {}) if isinstance(runtime, dict) else {}
    telemetry = runtime.get("boiler_telemetry", {}) if isinstance(runtime, dict) else {}
    current_day = ledger.get("days", {}).get(now.date().isoformat(), {}) if isinstance(ledger, dict) else {}
    planner_days = forecast.get("diagnostics", {}).get("boiler_daily_budget", {}) if isinstance(forecast, dict) else {}
    economics = boiler_decision.get("evidence", {}).get("economics", {}) if isinstance(boiler_decision, dict) else {}
    phase_evidence = boiler_decision.get("evidence", {}).get("phases", {}) if isinstance(boiler_decision, dict) else {}
    phase_baseline = boiler_decision.get("evidence", {}).get("robust_phase_baseline_kw", []) if isinstance(boiler_decision, dict) else []

    lines = [
        f"planner_v11 status (show_status.py v{VERSION})",
        f"čas: {now.isoformat(timespec='seconds')}",
        "",
        "== Forecast ==",
        f"generated_at: {forecast.get('generated_at') if isinstance(forecast, dict) else 'n/a'}",
        f"valid_until:  {forecast.get('valid_until') if isinstance(forecast, dict) else 'n/a'}",
        f"solver:       {forecast.get('solver', {}).get('status') if isinstance(forecast, dict) else 'n/a'}",
        f"dry_run:      {forecast.get('dry_run') if isinstance(forecast, dict) else 'n/a'}",
        "",
        "== Teď ==",
        f"SoC live:      {fmt(live.get('battery_soc'), '%')}",
        f"dům live:      {fmt(live.get('house_consumption'), ' W', 0)}",
        f"síť live:      {fmt_grid_flow(live.get('meter_active_power_total'))}",
        "síť fáze:      " + " | ".join(
            f"{phase} {fmt_grid_flow(live.get(key))}"
            for phase, key in (("L1", "meter_active_power1"), ("L2", "meter_active_power2"), ("L3", "meter_active_power3"))
        ),
        f"slot:          {current.get('slot_start') if current else 'n/a'}",
        f"cena:          {fmt(current.get('price_eur_mwh') if current else None, ' EUR/MWh')}",
        f"baterie:       {(current or {}).get('battery_action', 'n/a')} ({fmt((current or {}).get('battery_power_kw'), ' kW')})",
        f"bojler plán:   {fmt((current or {}).get('boiler_power_kw'), ' kW')}",
        "",
        "== Bojler: planner budget ==",
        f"denní limit:   {fmt(forecast.get('diagnostics', {}).get('boiler_opportunistic_daily_limit_kwh'), ' kWh', 2)}",
    ]
    for local_date, budget in sorted(planner_days.items()):
        lines.append(
            f"{local_date}: commanded={fmt(budget.get('commanded_before_plan_kwh'), ' kWh', 2)} "
            f"delivered={fmt(budget.get('estimated_delivered_before_plan_kwh'), ' kWh', 2)} "
            f"remaining={fmt(budget.get('remaining_planner_budget_kwh'), ' kWh', 2)} "
            f"hard={fmt(budget.get('planned_hard_kwh'), ' kWh', 2)} "
            f"opportunistic={fmt(budget.get('planned_opportunistic_kwh'), ' kWh', 2)}"
        )
    lines.extend([
        "",
        "== Bojler: executor ==",
        f"targety:       planner={boiler_decision.get('planned_phases', 'n/a')}f "
        f"realtime={boiler_decision.get('realtime_economic_phases', 'n/a')}f "
        f"final={boiler_decision.get('target_phases', 'n/a')}f",
        f"masky:         current={boiler_decision.get('current_mask', 'n/a')} target={boiler_decision.get('target_mask', 'n/a')} "
        f"confirmed={ledger.get('current_mask', 'n/a') if isinstance(ledger, dict) else 'n/a'}",
        f"stav/důvody:   {boiler_decision.get('status', 'n/a')} | {', '.join(boiler_decision.get('reasons', []))}",
        f"telemetrie:    samples={telemetry.get('sample_count', 0)} age={fmt(telemetry.get('latest_age_seconds'), ' s', 0)} "
        f"export min/med/latest/stable={fmt(telemetry.get('export_min_kw'), '', 2)}/"
        f"{fmt(telemetry.get('export_median_kw'), '', 2)}/{fmt(telemetry.get('export_latest_kw'), '', 2)}/"
        f"{fmt(telemetry.get('stable_export_kw'), ' kW', 2)}",
        f"pre-boiler:    {fmt(telemetry.get('reconstructed_pre_boiler_surplus_kw'), ' kW', 2)} "
        f"(confirmed delivery {fmt(telemetry.get('confirmed_boiler_delivery_kw'), ' kW', 2)})",
        f"ekonomika:     import={fmt(economics.get('import_cost_czk_kwh'), ' CZK/kWh', 2)} "
        f"export={fmt(economics.get('export_opportunity_czk_kwh'), ' CZK/kWh', 2)} "
        f"gas={fmt(economics.get('gas_heat_value_czk_kwh'), ' CZK/kWh', 2)}",
        f"future solar:  {economics.get('best_future_solar_opportunity_today')}",
        f"fáze:          baseline={phase_baseline} safe={phase_evidence.get('safe_phase_indices')} "
        f"max={fmt(phase_evidence.get('max_phase_load_kw'), ' kW', 2)} "
        f"rebalance_gain={fmt(phase_evidence.get('rebalance_improvement_kw'), ' kW', 2)}",
        f"ledger dnes:   commanded={fmt(current_day.get('commanded_kwh'), ' kWh', 3)} "
        f"estimated delivered={fmt(current_day.get('estimated_delivered_kwh'), ' kWh', 3)} "
        f"confidence={current_day.get('delivery_confidence', 'n/a')} source={current_day.get('delivery_source', 'n/a')}",
        "candidate table:",
    ])
    for candidate in economics.get("candidates", []):
        lines.append(
            f"  {candidate.get('phases')}f/{fmt(candidate.get('target_kw'), ' kW', 1)}: "
            f"surplus={fmt(candidate.get('surplus_covered_kw'), ' kW', 2)} "
            f"import={fmt(candidate.get('import_covered_kw'), ' kW', 2)} "
            f"mix={fmt(candidate.get('mixed_cost_czk_kwh'), ' CZK/kWh', 2)} "
            f"economic={candidate.get('economic')} future_better={candidate.get('future_solar_better_for_import')}"
        )
    lines.extend([
        "",
        "== Detekované zátěže ==",
        f"EV:            {fmt(detected.get('ev', {}).get('detected_kw'), ' kW')} ({detected.get('ev', {}).get('state', 'n/a')})",
        f"bazén:         {fmt(detected.get('pool', {}).get('detected_kw'), ' kW')} ({detected.get('pool', {}).get('reason', 'n/a')})",
        f"bojler:        {fmt(detected.get('boiler', {}).get('detected_kw'), ' kW')}",
        f"neočekávané:   {fmt(detected.get('unexpected_load', {}).get('kw'), ' kW')} active={detected.get('unexpected_load', {}).get('active')}",
        f"plan adjust:   {fmt(detected.get('planning_adjustment', {}).get('kw'), ' kW')} do {detected.get('planning_adjustment', {}).get('valid_until')}",
        "",
        f"== Změny plánu v příštích {hours:g} h ==",
    ])

    for slot in action_changes(slots if isinstance(slots, list) else [], now, hours, tz):
        lines.append(
            f"{slot.get('slot_start')} | bat={slot.get('battery_action')} {fmt(slot.get('battery_power_kw'), 'kW')} "
            f"| bojler={fmt(slot.get('boiler_power_kw'), 'kW')} | EV={fmt(slot.get('ev_load_kwh'), 'kWh', 2)}"
        )

    if full:
        lines.extend(["", "== Sloty =="])
        end = now + timedelta(hours=hours)
        for slot in slots if isinstance(slots, list) else []:
            slot_start = parse_dt(slot.get("slot_start"), tz)
            if slot_start is None or slot_start < now or slot_start > end:
                continue
            lines.append(
                f"{slot.get('slot_start')} price={fmt(slot.get('price_eur_mwh'))} "
                f"pv={fmt(slot.get('pv_estimate_kwh'), 'kWh', 2)} soc={fmt(slot.get('soc_start_pct'), '%')}->{fmt(slot.get('soc_end_pct'), '%')} "
                f"grid_imp={fmt(slot.get('grid_import_kwh'), 'kWh', 2)} grid_exp={fmt(slot.get('grid_export_kwh'), 'kWh', 2)}"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only human status for planner_v11")
    parser.add_argument("--hours", type=float, default=12.0, help="How many forecast hours to show")
    parser.add_argument("--full", action="store_true", help="Print all slots in the selected horizon")
    args = parser.parse_args()
    print(build_status(hours=args.hours, full=args.full))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())