#!/usr/bin/env python3
"""
 executor.py v11 - produkční taktická 5min vrstva nad `forecast_48h.json`.

Autoritativní zdroje:
  - ARCHITECTURE_DESIGN_v10_FINAL.md sekce 2.2, 12, 13, 15.2, 19.
  - CONTROL_LOGIC_SPEC_v10.yaml sekce `executor`, `files`, `failsafe`.
  - SERVER_IMPLEMENTATION_GUIDE_v10.md sekce 8/9/15/16.

 Rozsah této verze:
   - Baterie: vlastní všechny čtyři ECO kanály jako rolling kvarteto 15min
     oken. Zápis je celoblokový, s read-backem všech čtyř bloků.
  - Bojlerový target je omezen plánem, write gate, fail-safe stavem a fázovým
    headroomem. Low-level relay write/read-back adaptér existuje v `lib.relay`
    a executor jej zavolá pouze po vypnutí DRY_RUN a zapnutí `boiler_write_enabled`.
  - Export limit se nezapisuje; dle v10 guide zůstává vlastníkem stávající
    `check-power-out.sh`, executor jej v této verzi pouze reportuje.

 Fail-safe princip: hard request je povinné minimum; opportunistic target může
     realtime ekonomika rozšířit jen při bezpečném fázovém headroomu.

Changelog:
- v2.9 (2026-08-10): Interpolate planned SoC within the current slot, include
  actual SoC in deviation alerts, deduplicate changing residual values, and send
  retry-safe boiler-full and EV-session-closed completion notifications.
- v2.8 (2026-08-07): Persist direct-wallbox EV charging sessions, bind user or
  synthetic targets, and trigger idempotent detached replans at start/closure.
- v2.7 (2026-08-06): Raise the SoC deviation alert threshold to 15 percentage
  points and report direction in a concise human-readable one-line message.
- v2.6 (2026-08-03): Pass confirmed boiler telemetry/ledger context into live
  load detectors so relay-controlled boiler phases are not misclassified as
  `unexpected_load` during active heating.
- v2.5 (2026-08-03): Map HOLD to minimum ECO charge (-1 %) with 100 % SoC
  target instead of 0 % discharge. Real inverter test showed 0 % does not
  prevent self-use discharge, while charge 1 % approximates hold with about
  tens of watts.
- v2.4 (2026-08-03): Program ECO channels as merged active segments only;
  skip load-following DISCHARGE_TO_LOAD/SELF_USE slots instead of writing
  disabled 15-minute windows, so upcoming HOLD/charge/grid-discharge windows
  are visible and active in the inverter UI.
- v2.3 (2026-08-03): Prevent GoodWe ECO schedule blocks from crossing local
  midnight; the last 15-minute slot of a day is truncated to 23:59 to avoid
  inverter `ILLEGAL DATA VALUE` on 23:45-00:00 windows.
- v2.1 (2026-08-02): Production v11: explicit user-approved real device
  writes; HOLD is an enabled ECO slot with power_pct=0 and soc_pct=0.
- v1.5 (2026-07-27): Read relay `/98` health before every boiler decision,
  including DRY_RUN/no-change cycles; block a real relay write when unavailable.
- v1.4 (2026-07-24): Send deduplicated notify_admins alerts for inverter read
  failures, invalid/stale forecasts, relay read-back failures and active
  unexpected loads.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import planner as planner_module
from lib import alerting, boiler_state, detectors, economics, ev_session, inverter_client, request_store, telemetry, wallbox_client
from lib.config import Config, ConfigError, load_config


SCHEMA_VERSION = 10
VERSION = "2.9"
MODEL_VERSION = "11-executor-v1"

PLANNER_DIR = Path(__file__).resolve().parent
STATE_DIR = PLANNER_DIR / "state"
DEFAULT_CONFIG_PATH = PLANNER_DIR / "config.toml"
FORECAST_PATH = STATE_DIR / "forecast_48h.json"
RUNTIME_STATE_PATH = STATE_DIR / "runtime_state.json"
STATE_HISTORY_PATH = STATE_DIR / "state_history.jsonl"
DETECTED_LOADS_PATH = STATE_DIR / "detected_loads.json"
ALERT_STATE_PATH = STATE_DIR / "alert_state.json"
ECO_PLAN_STATE_PATH = STATE_DIR / "eco_plan_state.json"
DEVICE_FAILURE_STATE_PATH = STATE_DIR / "device_failure_state.json"
BOILER_CONTROL_STATE_PATH = STATE_DIR / "boiler_control_state.json"
EV_SESSION_STATE_PATH = STATE_DIR / "ev_session_state.json"
REQUESTS_PATH = STATE_DIR / "requests.json"
PLANNER_REPLAN_LOG_PATH = PLANNER_DIR.parent / "logs" / "planner_v11.log"

PHASE_ORDER = ("L1", "L2", "L3")
PHASE_CURRENT_KEYS = {
    "L1": ("igrid1", "meter_active_power1"),
    "L2": ("igrid2", "meter_active_power2"),
    "L3": ("igrid3", "meter_active_power3"),
}
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def log(message: str, *, verbose: bool = True) -> None:
    if verbose:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}")


def atomic_write_json(path: Path, payload: dict) -> None:
    planner_module.atomic_write_json(path, payload)


def read_json(path: Path, default: Any) -> Any:
    return planner_module.read_json(path, default)


def parse_iso_datetime(value: str, tz: ZoneInfo) -> datetime:
    return planner_module.parse_iso_datetime(value, tz)


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def read_recent_history(path: Path, max_records: int = 12) -> list[dict]:
    """Read the last runtime history records without requiring external tools."""
    if max_records <= 0:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max_records:]
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def load_forecast(path: Path = FORECAST_PATH) -> Optional[dict]:
    doc = read_json(path, None)
    return doc if isinstance(doc, dict) else None


def validate_forecast(doc: Optional[dict], cfg: Config, now: datetime) -> tuple[bool, list[str]]:
    """Vrátí (valid, reasons). Nevalidní plán znamená fail-safe."""
    reasons: list[str] = []
    if doc is None:
        return False, ["FORECAST_MISSING_OR_INVALID_JSON"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        reasons.append("FORECAST_SCHEMA_VERSION_INVALID")
    if doc.get("config_hash") != cfg.config_hash:
        reasons.append("FORECAST_CONFIG_HASH_MISMATCH")
    if doc.get("solver", {}).get("status") != "optimal":
        reasons.append("FORECAST_SOLVER_NOT_OPTIMAL")
    if not isinstance(doc.get("slots"), list) or not doc.get("slots"):
        reasons.append("FORECAST_SLOTS_MISSING")

    tz = ZoneInfo(cfg.system.timezone)
    try:
        generated_at = parse_iso_datetime(doc.get("generated_at", ""), tz)
        valid_until = parse_iso_datetime(doc.get("valid_until", ""), tz)
    except (TypeError, ValueError):
        reasons.append("FORECAST_TIMESTAMPS_INVALID")
    else:
        if now > valid_until:
            reasons.append("FORECAST_STALE_VALID_UNTIL")
        age_minutes = (now - generated_at).total_seconds() / 60.0
        if age_minutes > cfg.system.plan_max_age_minutes:
            reasons.append("FORECAST_STALE_MAX_AGE")
        if age_minutes < -5:
            reasons.append("FORECAST_GENERATED_IN_FUTURE")

    return not reasons, reasons


def round_down_to_slot(dt: datetime, slot_minutes: int) -> datetime:
    return planner_module.round_down_to_slot(dt, slot_minutes)


def find_current_slot(slots: list[dict], now: datetime, cfg: Config) -> Optional[dict]:
    tz = ZoneInfo(cfg.system.timezone)
    rounded = round_down_to_slot(now, cfg.system.planning_step_minutes)
    for slot in slots:
        raw = slot.get("slot_start")
        if not isinstance(raw, str):
            continue
        try:
            slot_start = parse_iso_datetime(raw, tz)
        except ValueError:
            continue
        if slot_start == rounded:
            return slot
    return None


def planned_boiler_phases(slot: Optional[dict], cfg: Config) -> int:
    if not slot:
        return 0
    power_kw = float(slot.get("boiler_power_kw", 0.0) or 0.0)
    if power_kw <= 0:
        return 0
    phases = round(power_kw / cfg.boiler.phase_power_kw)
    return max(0, min(cfg.boiler.phase_count, phases))


def planned_battery_action(slot: Optional[dict]) -> dict:
    if not slot:
        return {"action": "NO_PLAN", "battery_power_kw": 0.0}
    return {
        "action": slot.get("battery_action", "UNKNOWN"),
        "battery_power_kw": float(slot.get("battery_power_kw", 0.0) or 0.0),
    }


def _slot_at(slots: list[dict], when: datetime, cfg: Config) -> Optional[dict]:
    tz = ZoneInfo(cfg.system.timezone)
    for slot in slots:
        try:
            if parse_iso_datetime(str(slot.get("slot_start", "")), tz) == when:
                return slot
        except ValueError:
            continue
    return None


def _empty_eco_schedule(channel: int) -> dict:
    return {
        "channel": channel,
        "start_h": 0, "start_m": 0, "end_h": 0, "end_m": 0,
        "day_names": list(inverter_client.ALL_DAYS),
        "power_pct": 0, "soc_pct": 0, "enabled": False,
        "action": "EMPTY",
    }


def eco_end_for_slot(when: datetime, cfg: Config) -> datetime:
    """Return a GoodWe-safe ECO end time for one planning slot.

    The inverter rejects a single ECO block that crosses local midnight, even
    when the block is disabled. Represent the final planning slot of the day as
    23:45-23:59 instead of 23:45-00:00; the next day's first block starts at
    00:00 and uses the next weekday bit.
    """
    end = when + timedelta(minutes=cfg.system.planning_step_minutes)
    if end.date() != when.date():
        return when.replace(hour=23, minute=59, second=0, microsecond=0)
    return end


def eco_schedule_for_slot(channel: int, when: datetime, slot: Optional[dict], cfg: Config) -> dict:
    """Translate one planned 15min slot into the owned full ECO schedule block."""
    if not slot:
        return _empty_eco_schedule(channel)
    action = str(slot.get("battery_action") or "SELF_USE")
    power_kw = abs(float(slot.get("battery_power_kw", 0.0) or 0.0))
    end = eco_end_for_slot(when, cfg)
    result = {
        "channel": channel,
        "start_h": when.hour, "start_m": when.minute,
        "end_h": end.hour, "end_m": end.minute,
        "day_names": [WEEKDAY_NAMES[when.weekday()]],
        "power_pct": 0, "soc_pct": 0, "enabled": False,
        "action": action,
        "slot_start": when.isoformat(),
    }
    if action == "FORCE_CHARGE":
        requested_kw = min(max(power_kw, 0.1), cfg.battery.max_charge_kw)
        result["power_pct"] = -max(1, min(100, round(requested_kw * 10)))
        # GoodWe may overshoot its integer SoC target by one point.
        planned_target = float(slot.get("soc_end_pct", cfg.battery.max_soc_grid_pct) or cfg.battery.max_soc_grid_pct)
        result["soc_pct"] = max(0, min(100, int(round(min(planned_target, cfg.battery.max_soc_grid_pct - 1)))))
        result["enabled"] = True
    elif action == "DISCHARGE_TO_GRID":
        requested_kw = min(max(power_kw, 0.1), min(cfg.battery.max_discharge_kw, 7.0))
        result["power_pct"] = max(1, min(100, round(requested_kw * 10)))
        result["enabled"] = True
    elif action == "HOLD":
        # Real inverter test (2026-08-03): 0% ECO does not block self-use
        # discharge. Minimum charge (-1%) is the closest practical hold and
        # draws only tens of watts.
        result["power_pct"] = -1
        result["soc_pct"] = 100
        result["enabled"] = True
    # SELF_USE and DISCHARGE_TO_LOAD deliberately remain disabled: positive ECO
    # would force a fixed battery output and is not a load-following mode.
    return result


def _same_eco_segment(left: dict, right: dict) -> bool:
    """Return True when two per-slot ECO schedules can be merged safely."""
    keys = ("action", "day_names", "power_pct", "soc_pct", "enabled")
    return all(left.get(key) == right.get(key) for key in keys)


def _iter_forecast_slots_from(forecast_doc: dict, start: datetime, cfg: Config):
    tz = ZoneInfo(cfg.system.timezone)
    slots = forecast_doc.get("slots", []) if isinstance(forecast_doc, dict) else []
    parsed: list[tuple[datetime, dict]] = []
    for slot in slots:
        try:
            when = parse_iso_datetime(str(slot.get("slot_start", "")), tz)
        except ValueError:
            continue
        if when >= start:
            parsed.append((when, slot))
    parsed.sort(key=lambda item: item[0])
    return parsed


def build_eco_quartet(forecast_doc: dict, now: datetime, cfg: Config) -> list[dict]:
    """Build the four GoodWe ECO channels as upcoming active segments.

    Load-following actions (`SELF_USE`, `DISCHARGE_TO_LOAD`) intentionally do
    not need active ECO timers. They are skipped, and channels are reserved for
    the nearest active ECO segments (`HOLD`, `FORCE_CHARGE`,
    `DISCHARGE_TO_GRID`). Adjacent slots with identical ECO parameters are
    merged into one timer, without crossing local midnight.
    """
    start = round_down_to_slot(now, cfg.system.planning_step_minutes)
    step = timedelta(minutes=cfg.system.planning_step_minutes)
    parsed = _iter_forecast_slots_from(forecast_doc, start, cfg)
    by_start = {when: slot for when, slot in parsed}
    channel = 1
    schedules: list[dict] = []
    idx = 0
    while idx < len(parsed) and channel <= 4:
        when, slot = parsed[idx]
        per_slot = eco_schedule_for_slot(channel, when, slot, cfg)
        if not per_slot.get("enabled"):
            idx += 1
            continue

        segment = dict(per_slot)
        next_when = when + step
        while next_when in by_start:
            candidate = eco_schedule_for_slot(channel, next_when, by_start[next_when], cfg)
            if not candidate.get("enabled") or not _same_eco_segment(segment, candidate):
                break
            segment["end_h"] = candidate["end_h"]
            segment["end_m"] = candidate["end_m"]
            next_when = next_when + step

        schedules.append(segment)
        channel += 1
        while idx < len(parsed) and parsed[idx][0] < next_when:
            idx += 1

    while channel <= 4:
        schedules.append(_empty_eco_schedule(channel))
        channel += 1
    return schedules


def _eco_plan_fingerprint(schedules: list[dict]) -> list[dict]:
    keys = ("channel", "start_h", "start_m", "end_h", "end_m", "day_names", "power_pct", "soc_pct", "enabled", "action", "slot_start")
    return [{key: item.get(key) for key in keys} for item in schedules]


def _eco_minutes(item: dict, prefix: str) -> Optional[int]:
    try:
        hour = int(item[f"{prefix}_h"])
        minute = int(item[f"{prefix}_m"])
    except (KeyError, TypeError, ValueError):
        return None
    return hour * 60 + minute


def _eco_existing_covers_wanted(existing: dict, wanted: dict) -> bool:
    """Return True when an already programmed active segment covers wanted.

    After merging adjacent active slots, an existing timer may begin earlier
    than the newly computed segment while still covering the current need. This
    avoids unnecessary rewrites every 15 minutes during a longer HOLD/charge
    window.
    """
    if not wanted.get("enabled"):
        meaningful = ("start_h", "start_m", "end_h", "end_m", "day_names", "power_pct", "soc_pct", "enabled", "action", "slot_start")
        return all(existing.get(key) == wanted.get(key) for key in meaningful)
    same_mode = all(
        existing.get(key) == wanted.get(key)
        for key in ("day_names", "power_pct", "soc_pct", "enabled", "action")
    )
    if not same_mode:
        return False
    existing_start = _eco_minutes(existing, "start")
    existing_end = _eco_minutes(existing, "end")
    wanted_start = _eco_minutes(wanted, "start")
    wanted_end = _eco_minutes(wanted, "end")
    if None in (existing_start, existing_end, wanted_start, wanted_end):
        return False
    return existing_start <= wanted_start and existing_end >= wanted_end


def eco_plan_needs_write(previous: dict, desired: list[dict], forecast_doc: dict) -> tuple[bool, str]:
    """Rewrite on a new forecast; otherwise preserve a matching rolling quartet."""
    if not isinstance(previous, dict):
        return True, "NO_PREVIOUS_ECO_PLAN"
    if previous.get("forecast_generated_at") != forecast_doc.get("generated_at"):
        return True, "FORECAST_REPLAN"
    old = previous.get("schedules")
    if not isinstance(old, list) or len(old) != 4:
        return True, "PREVIOUS_ECO_PLAN_INVALID"
    # A channel is not tied to a particular chronological position. After one
    # slot expires, e.g. former channel 2 can still be today's current window.
    # Retain the existing quartet precisely when both current and next planned
    # windows have a matching still-programmed schedule; this avoids a needless
    # rewrite at every 15-minute boundary.
    for wanted in desired[:2]:
        if not any(_eco_existing_covers_wanted(existing, wanted) for existing in old):
            return True, "CURRENT_OR_NEXT_ECO_WINDOW_CHANGED"
    return False, "CURRENT_AND_NEXT_WINDOW_MATCH"


def execute_battery_plan(schedules: list[dict], cfg: Config) -> dict:
    return asyncio.run(inverter_client.write_eco_modes(schedules, dry_run=False))


def decide_battery_execution(
    forecast_doc: Optional[dict], slot: Optional[dict], cfg: Config, forecast_valid: bool,
    now: datetime, eco_plan_state_path: Path = ECO_PLAN_STATE_PATH,
) -> dict:
    planned = planned_battery_action(slot)
    if not forecast_valid or forecast_doc is None:
        return {**planned, "execute": False, "status": "blocked_failsafe_stale_or_invalid_plan"}
    desired = build_eco_quartet(forecast_doc, now, cfg)
    previous = read_json(eco_plan_state_path, {})
    needs_write, reason = eco_plan_needs_write(previous, desired, forecast_doc)
    decision = {**planned, "schedules": desired, "rewrite_reason": reason, "execute": False}
    if not needs_write:
        return {**decision, "status": "eco_plan_retained"}
    if cfg.system.dry_run or not cfg.system.battery_write_enabled:
        return {**decision, "status": "blocked_by_dry_run_or_write_gate"}
    try:
        adapter_result = execute_battery_plan(desired, cfg)
    except Exception as exc:  # device failures are counted/alerted separately
        return {**decision, "status": "eco_write_failed", "error": str(exc)}
    status = "eco_plan_written" if adapter_result.get("status") == "written" else "eco_write_verification_failed"
    if status == "eco_plan_written":
        atomic_write_json(eco_plan_state_path, {
            "schema_version": SCHEMA_VERSION,
            "written_at": now.isoformat(),
            "forecast_generated_at": forecast_doc.get("generated_at"),
            "schedules": _eco_plan_fingerprint(desired),
            "adapter_result": adapter_result,
        })
    return {**decision, "execute": True, "status": status, "adapter_result": adapter_result}


def update_device_failure_counter(
    device: str,
    healthy: bool,
    *,
    now: datetime,
    state_path: Path = DEVICE_FAILURE_STATE_PATH,
) -> dict:
    """Persist consecutive device failures; a success resets only its counter."""
    state = read_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    devices = state.setdefault("devices", {})
    old = devices.get(device) if isinstance(devices.get(device), dict) else {}
    count = 0 if healthy else int(old.get("consecutive_failures", 0) or 0) + 1
    entry = {
        "consecutive_failures": count,
        "healthy": healthy,
        "updated_at": now.isoformat(),
    }
    if not healthy:
        entry["first_failed_at"] = old.get("first_failed_at") or now.isoformat()
    devices[device] = entry
    state["schema_version"] = SCHEMA_VERSION
    atomic_write_json(state_path, state)
    return entry


def phase_current_from_live(live_state: dict, phase: str, cfg: Config) -> Optional[float]:
    """Vrátí odhad aktuálního proudu fáze v A, pokud dostupný.

    Preferuje `igridN` z GoodWe runtime dat. Pokud chybí, použije absolutní
    hodnotu fázového výkonu / nominální napětí. Pokud chybí obojí, vrátí None
    a executor headroom pro danou fázi fail-safe nepovolí.
    """
    current_key, power_key = PHASE_CURRENT_KEYS[phase]
    current = live_state.get(current_key)
    if current is not None:
        try:
            return abs(float(current))
        except (TypeError, ValueError):
            pass
    power_w = live_state.get(power_key)
    if power_w is not None:
        try:
            return abs(float(power_w)) / cfg.grid.phase_nominal_voltage_v
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return None


def additional_current_for_boiler_phase(cfg: Config) -> float:
    return cfg.boiler.phase_power_kw * 1000.0 / cfg.grid.phase_nominal_voltage_v


def safe_boiler_phases_by_headroom(
    planned_phases: int, live_state: dict, cfg: Config
) -> tuple[int, list[str], dict]:
    """Omezí plánovaný počet fází podle dostupného fázového headroomu."""
    reasons: list[str] = []
    evidence: dict[str, Any] = {}
    if planned_phases <= 0:
        return 0, ["BOILER_PLAN_OFF"], evidence

    add_a = additional_current_for_boiler_phase(cfg)
    safe = 0
    for phase in PHASE_ORDER[: planned_phases]:
        current_a = phase_current_from_live(live_state, phase, cfg)
        evidence[phase] = {"measured_current_a": current_a, "additional_current_a": add_a}
        if current_a is None:
            reasons.append(f"{phase}_CURRENT_UNAVAILABLE")
            break
        if current_a + add_a <= cfg.grid.soft_phase_limit_a:
            safe += 1
            reasons.append(f"{phase}_HEADROOM_OK")
        else:
            reasons.append(f"{phase}_HEADROOM_BLOCKED")
            break
    if safe < planned_phases:
        reasons.append("BOILER_REDUCED_FOR_PHASE_HEADROOM")
    return safe, reasons, evidence


def relay_mask_from_health(relay_health: Optional[dict]) -> tuple[bool, bool, bool]:
    parsed = (relay_health or {}).get("parsed", {})
    return tuple(bool(parsed.get(f"phase{i}")) for i in (1, 2, 3))


def best_future_solar_opportunity_today(forecast_doc: Optional[dict], now: datetime, cfg: Config) -> Optional[dict]:
    candidates = []
    for slot in (forecast_doc or {}).get("slots", []):
        try:
            start = parse_iso_datetime(str(slot.get("slot_start", "")), ZoneInfo(cfg.system.timezone))
            surplus_kwh = float(slot.get("pv_estimate_kwh", 0.0) or 0.0) - float(slot.get("fixed_load_kwh", 0.0) or 0.0)
            opportunity = float(slot.get("export_revenue_czk_kwh", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if start > now and start.date() == now.date() and surplus_kwh > 0.05:
            candidates.append((opportunity, start, surplus_kwh))
    if not candidates:
        return None
    opportunity, start, surplus_kwh = min(candidates, key=lambda item: item[0])
    return {"slot_start": start.isoformat(), "opportunity_cost_czk_kwh": opportunity, "surplus_kwh": surplus_kwh}


def economic_boiler_candidates(
    *, slot: Optional[dict], forecast_doc: Optional[dict], now: datetime, cfg: Config, telemetry_evidence: dict,
) -> dict:
    gas_value = economics.gas_heat_value_czk_per_kwh(cfg)
    spot = float((slot or {}).get("price_eur_mwh", 0.0) or 0.0)
    import_cost = economics.import_cost_czk_per_kwh(spot, cfg)
    export_opportunity = economics.export_revenue_czk_per_kwh(spot, cfg)
    pre_surplus = max(0.0, float(telemetry_evidence.get("reconstructed_pre_boiler_surplus_kw", 0.0) or 0.0))
    future = best_future_solar_opportunity_today(forecast_doc, now, cfg)
    future_cost = future.get("opportunity_cost_czk_kwh") if future else None
    rows = []
    best = 0
    for phases in range(cfg.boiler.phase_count + 1):
        target_kw = phases * cfg.boiler.phase_power_kw
        surplus_kw = min(target_kw, pre_surplus)
        import_kw = max(0.0, target_kw - surplus_kw)
        mixed_cost = 0.0 if target_kw == 0 else (surplus_kw * export_opportunity + import_kw * import_cost) / target_kw
        future_better = import_kw > 0 and future_cost is not None and future_cost + 1e-9 < import_cost
        economic = target_kw == 0 or (mixed_cost < gas_value and not future_better)
        rows.append({
            "phases": phases, "target_kw": target_kw, "surplus_covered_kw": round(surplus_kw, 6),
            "import_covered_kw": round(import_kw, 6), "mixed_cost_czk_kwh": round(mixed_cost, 6),
            "economic": economic, "future_solar_better_for_import": future_better,
        })
        if economic:
            best = phases
    return {
        "target_phases": best,
        "gas_heat_value_czk_kwh": round(gas_value, 6),
        "import_cost_czk_kwh": round(import_cost, 6),
        "export_opportunity_czk_kwh": round(export_opportunity, 6),
        "best_future_solar_opportunity_today": future,
        "candidates": rows,
    }


def _minutes_since(raw: Any, now: datetime) -> float:
    try:
        value = parse_iso_datetime(str(raw), ZoneInfo(str(now.tzinfo)))
    except (TypeError, ValueError):
        return 1e9
    return max(0.0, (now - value).total_seconds() / 60.0)


def _parse_optional_datetime(raw: Any, tz: ZoneInfo) -> Optional[datetime]:
    try:
        return parse_iso_datetime(str(raw), tz) if raw else None
    except (TypeError, ValueError):
        return None


def select_phase_mask(
    *, target_phases: int, current_mask: tuple[bool, bool, bool], baseline_kw: list[float], ledger: dict, now: datetime, cfg: Config,
) -> tuple[tuple[bool, bool, bool], list[str], dict]:
    reasons = []
    max_phase_kw = cfg.grid.soft_phase_limit_a * cfg.grid.phase_nominal_voltage_v / 1000.0
    safe_indices = [idx for idx, base in enumerate(baseline_kw) if base + cfg.boiler.phase_power_kw <= max_phase_kw]
    desired_indices = sorted(safe_indices, key=lambda idx: (baseline_kw[idx], idx))[:target_phases]
    desired = tuple(idx in desired_indices for idx in range(3))
    if len(desired_indices) < target_phases:
        reasons.append("BOILER_REDUCED_FOR_PHASE_HEADROOM")

    last_on = ledger.get("phase_last_on_at", [None, None, None])
    last_off = ledger.get("phase_last_off_at", [None, None, None])
    mutable = list(desired)
    for idx in range(3):
        phase_is_safe = idx in safe_indices
        if current_mask[idx] and not mutable[idx] and phase_is_safe and _minutes_since(last_on[idx], now) < cfg.boiler.minimum_on_minutes:
            mutable[idx] = True
            reasons.append(f"L{idx + 1}_MINIMUM_ON_HOLD")
        if not current_mask[idx] and mutable[idx] and _minutes_since(last_off[idx], now) < cfg.boiler.minimum_off_minutes:
            mutable[idx] = False
            reasons.append(f"L{idx + 1}_MINIMUM_OFF_HOLD")
    desired = tuple(mutable)

    if sum(desired) == sum(current_mask) and desired != current_mask and sum(desired) > 0:
        current_score = sum(baseline_kw[idx] for idx, enabled in enumerate(current_mask) if enabled)
        desired_score = sum(baseline_kw[idx] for idx, enabled in enumerate(desired) if enabled)
        improvement = current_score - desired_score
        current_is_safe = all(not enabled or idx in safe_indices for idx, enabled in enumerate(current_mask))
        if current_is_safe and improvement < cfg.boiler.rebalance_hysteresis_kw:
            desired = current_mask
            reasons.append("BOILER_REBALANCE_SUPPRESSED_BY_HYSTERESIS")
    else:
        improvement = None
    return desired, reasons, {
        "max_phase_load_kw": round(max_phase_kw, 6), "safe_phase_indices": safe_indices,
        "rebalance_improvement_kw": None if improvement is None else round(improvement, 6),
    }


def execute_boiler_target(target_mask: tuple[bool, bool, bool], cfg: Config) -> dict:
    """Apply boiler target via the relay adapter. Called only after write gates."""
    from lib import relay  # noqa: PLC0415

    return relay.apply_phase_mask(
        target_mask,
        dry_run=False,
        verify_delay_s=cfg.boiler.relay_verify_delay_seconds,
    )


def check_relay_health() -> dict:
    """Read the boiler relay status independently of a target change or write gate."""
    from lib import relay  # noqa: PLC0415

    try:
        pole = relay.read_status_pole()
        parsed = relay.parse_pole(pole)
    except Exception as exc:  # noqa: BLE001 - device reachability must not abort executor.
        return {"status": "relay_status_read_failed", "relay_status_ok": False, "error": str(exc)}
    if any(value is None for value in parsed.values()):
        return {
            "status": "relay_status_read_failed",
            "relay_status_ok": False,
            "pole": pole,
            "parsed": parsed,
        }
    return {"status": "relay_status_ok", "relay_status_ok": True, "pole": pole, "parsed": parsed}


def decide_boiler_execution(
    slot: Optional[dict],
    live_state: dict,
    cfg: Config,
    forecast_valid: bool,
    relay_health: Optional[dict] = None,
    forecast_doc: Optional[dict] = None,
    now: Optional[datetime] = None,
    telemetry_evidence: Optional[dict] = None,
    ledger: Optional[dict] = None,
) -> dict:
    now = now or datetime.now(ZoneInfo(cfg.system.timezone))
    telemetry_evidence = telemetry_evidence or {}
    ledger = boiler_state.normalize_state(ledger)
    planned = planned_boiler_phases(slot, cfg)
    current_mask = relay_mask_from_health(relay_health)
    if not forecast_valid:
        target_mask = (False, False, False)
        base = {
            "planned_phases": planned, "realtime_economic_phases": 0, "target_phases": 0,
            "current_mask": list(current_mask), "target_mask": list(target_mask),
            "reasons": ["BOILER_FAILSAFE_OFF"],
        }
        if relay_health is not None and relay_health.get("relay_status_ok") is False:
            return {**base, "execute": False, "status": "blocked_relay_health_check_failed", "relay_health": relay_health}
        if cfg.system.dry_run or not cfg.system.boiler_write_enabled:
            return {**base, "execute": False, "status": "blocked_failsafe_off_by_dry_run_or_write_gate"}
        adapter_result = execute_boiler_target(target_mask, cfg)
        relay_ok = (
            adapter_result.get("relay_status_ok") is True
            and adapter_result.get("verified") is not False
            and adapter_result.get("status") not in ("status_read_failed", "verify_read_failed", "write_verification_failed")
        )
        return {
            **base, "execute": True,
            "status": "failsafe_off_relay_written" if relay_ok else "relay_write_verification_failed",
            "adapter_result": adapter_result,
        }
    if relay_health is not None and relay_health.get("relay_status_ok") is False:
        return {
            "planned_phases": planned,
            "target_phases": 0,
            "execute": False,
            "status": "blocked_relay_health_check_failed",
            "reasons": ["RELAY_STATUS_UNAVAILABLE"],
            "relay_health": relay_health,
        }
    hard_active = float((slot or {}).get("boiler_hard_kwh", 0.0) or 0.0) > 1e-6
    economics_result = economic_boiler_candidates(
        slot=slot, forecast_doc=forecast_doc, now=now, cfg=cfg, telemetry_evidence=telemetry_evidence,
    )
    realtime = economics_result["target_phases"]
    requested = max(planned if hard_active else 0, realtime)
    baseline = telemetry_evidence.get("robust_phase_baseline_kw")
    if not isinstance(baseline, list) or len(baseline) != 3:
        baseline = [
            max(0.0, float(live_state.get(f"load_p{i}", 0.0) or 0.0) / 1000.0 - (cfg.boiler.phase_power_kw if current_mask[i - 1] else 0.0))
            for i in (1, 2, 3)
        ]
    target_mask, mask_reasons, phase_evidence = select_phase_mask(
        target_phases=requested, current_mask=current_mask, baseline_kw=baseline, ledger=ledger, now=now, cfg=cfg,
    )
    reasons = (["BOILER_HARD_MINIMUM"] if hard_active else []) + ["BOILER_REALTIME_ECONOMIC_EVALUATED"] + mask_reasons
    safe = sum(target_mask)
    evidence = {"telemetry": telemetry_evidence, "economics": economics_result, "phases": phase_evidence, "robust_phase_baseline_kw": baseline}
    if cfg.system.dry_run or not cfg.system.boiler_write_enabled:
        return {
            "planned_phases": planned,
            "realtime_economic_phases": realtime,
            "target_phases": safe,
            "current_mask": list(current_mask),
            "target_mask": list(target_mask),
            "execute": False,
            "status": "blocked_by_dry_run_or_write_gate",
            "reasons": reasons,
            "evidence": evidence,
        }
    adapter_result = execute_boiler_target(target_mask, cfg)
    relay_ok = (
        adapter_result.get("relay_status_ok") is True
        and adapter_result.get("verified") is not False
        and adapter_result.get("status") not in ("status_read_failed", "verify_read_failed", "write_verification_failed")
    )
    return {
        "planned_phases": planned,
        "realtime_economic_phases": realtime,
        "target_phases": safe,
        "current_mask": list(current_mask),
        "target_mask": list(target_mask),
        "execute": True,
        "status": "relay_written" if relay_ok else "relay_write_verification_failed",
        "reasons": reasons,
        "evidence": evidence,
        "adapter_result": adapter_result,
    }


def expected_soc_at(slot: dict, now: Optional[datetime], cfg: Config) -> Optional[float]:
    """Linearly interpolate planned SoC inside the current 15-minute slot."""

    start_soc = slot.get("soc_start_pct")
    end_soc = slot.get("soc_end_pct")
    if start_soc is None:
        return None
    if end_soc is None or now is None or not slot.get("slot_start"):
        return float(start_soc)
    try:
        slot_start = parse_iso_datetime(str(slot["slot_start"]), ZoneInfo(cfg.system.timezone))
    except (TypeError, ValueError):
        return float(start_soc)
    fraction = (now - slot_start).total_seconds() / (cfg.system.planning_step_minutes * 60.0)
    fraction = min(1.0, max(0.0, fraction))
    return float(start_soc) + (float(end_soc) - float(start_soc)) * fraction


def detect_plan_deviation(
    slot: Optional[dict], live_state: dict, cfg: Config, now: Optional[datetime] = None,
) -> tuple[bool, str]:
    if not slot:
        return False, "NO_CURRENT_SLOT"
    actual_soc = live_state.get("battery_soc")
    expected_soc = expected_soc_at(slot, now, cfg)
    if actual_soc is None or expected_soc is None:
        return False, "SOC_COMPARISON_UNAVAILABLE"
    signed_deviation = float(actual_soc) - float(expected_soc)
    deviation = abs(signed_deviation)
    direction = "ABOVE" if signed_deviation >= 0.0 else "BELOW"
    # Konfigurovaný diagnostický práh slouží jen pro alert/report, ne jako action.
    if deviation >= cfg.alerts.soc_deviation_threshold_pct_points:
        return True, f"SOC_DEVIATION_{direction}_{deviation:.1f}_PCT_POINTS"
    return False, f"SOC_DEVIATION_OK_{direction}_{deviation:.1f}_PCT_POINTS"


def soc_deviation_alert_message(deviation_reason: str, actual_soc: Any = None) -> str:
    """Format the machine SoC reason as one concise Czech alert line."""

    parts = str(deviation_reason or "").split("_")
    if len(parts) >= 6 and parts[:2] == ["SOC", "DEVIATION"]:
        direction = parts[2]
        value = parts[3]
        direction_cs = {"ABOVE": "nad", "BELOW": "pod"}.get(direction)
        if direction_cs is not None:
            current = ""
            try:
                current = f" (aktuálně {float(actual_soc):.0f}%)"
            except (TypeError, ValueError):
                pass
            return f"FVE ALERT: významná odchylka: SOC je o {value} % {direction_cs} plánem{current}"
    return f"FVE ALERT: významná odchylka: {deviation_reason}"


def detect_boiler_full_completion(
    ledger: dict, telemetry_evidence: dict, *, now: datetime,
) -> dict:
    """Detect the first robust thermostat stop of the local day."""

    day = boiler_state.today_entry(ledger, now.date())
    previous_kw = float(day.get("previous_confirmed_delivery_kw", 0.0) or 0.0)
    confirmed_raw = telemetry_evidence.get("confirmed_boiler_delivery_kw")
    sample_count = int(telemetry_evidence.get("sample_count", 0) or 0)
    current_mask = ledger.get("current_mask", [])
    try:
        confirmed_kw = float(confirmed_raw)
    except (TypeError, ValueError):
        confirmed_kw = 0.0
    detected_now = (
        not day.get("full_detected_at")
        and sample_count >= 3
        and any(bool(value) for value in current_mask[:3])
        and previous_kw >= 1.0
        and confirmed_kw <= 0.25
    )
    if detected_now:
        day["full_detected_at"] = now.isoformat()
    day["previous_confirmed_delivery_kw"] = round(confirmed_kw, 6)
    return {
        "detected_now": detected_now,
        "detected_at": day.get("full_detected_at"),
        "notification_sent_at": day.get("full_notification_sent_at"),
        "estimated_delivered_kwh": round(float(day.get("estimated_delivered_kwh", 0.0) or 0.0), 3),
        "previous_confirmed_delivery_kw": round(previous_kw, 3),
        "confirmed_delivery_kw": round(confirmed_kw, 3),
        "sample_count": sample_count,
    }


def completion_notification_candidates(*, now: datetime, ledger: dict, session_state: dict) -> list[dict]:
    """Build retry-safe one-shot completion notifications from persisted state."""

    candidates: list[dict] = []
    day = boiler_state.today_entry(ledger, now.date())
    if day.get("full_detected_at") and not day.get("full_notification_sent_at"):
        delivered = float(day.get("estimated_delivered_kwh", 0.0) or 0.0)
        candidates.append({
            "kind": "boiler_full",
            "key": f"executor.boiler_full.{now.date().isoformat()}",
            "message": f"Bojler je nahřátý naplno, dnes spotřeboval zhruba {delivered:.1f} kWh.",
        })
    if (
        isinstance(session_state, dict)
        and session_state.get("state") == "CLOSED"
        and session_state.get("session_id")
        and not session_state.get("completion_notification_sent_at")
    ):
        delivered = float(session_state.get("delivered_kwh", 0.0) or 0.0)
        candidates.append({
            "kind": "ev_closed",
            "key": f"executor.ev_closed.{session_state['session_id']}",
            "message": f"Auto je nabité, spotřeba {delivered:.1f} kWh.",
        })
    return candidates


def detect_runtime_loads(
    *,
    now: datetime,
    cfg: Config,
    live_state: dict,
    current_slot: Optional[dict],
    boiler_ledger: Optional[dict] = None,
    telemetry_evidence: Optional[dict] = None,
    history_path: Path = STATE_HISTORY_PATH,
    detector_path: Path = DETECTED_LOADS_PATH,
    wallbox_state: Optional[dict] = None,
) -> dict:
    recent_history = read_recent_history(history_path)
    previous_state = read_json(detector_path, {})
    return detectors.detect_loads(
        now=now,
        cfg=cfg,
        live_state=live_state,
        current_slot=current_slot,
        recent_history=recent_history,
        previous_state=previous_state if isinstance(previous_state, dict) else {},
        wallbox_state=wallbox_state,
        boiler_ledger=boiler_ledger if isinstance(boiler_ledger, dict) else {},
        telemetry_evidence=telemetry_evidence if isinstance(telemetry_evidence, dict) else {},
    )


def read_wallbox_state_for_detection() -> dict:
    """Read wallbox state for EV detection; never raises to executor."""

    return wallbox_client.read_wallbox_state().as_dict()


def active_ev_request(now: datetime, path: Path = REQUESTS_PATH) -> Optional[dict]:
    for item in request_store.active_requests(path, now=now):
        if isinstance(item, dict) and item.get("type") == "ev_charge":
            return item
    return None


def trigger_ev_session_replan(
    *,
    now: datetime,
    session_path: Path = EV_SESSION_STATE_PATH,
    planner_script: Path = PLANNER_DIR / "planner.py",
    log_path: Path = PLANNER_REPLAN_LOG_PATH,
) -> dict:
    """Claim and start one detached read-only planner process."""

    claimed, state = ev_session.claim_replan(session_path, now=now)
    outcome = {
        "claimed": claimed,
        "started": False,
        "reason": state.get("last_replan_reason") or state.get("replan_reason"),
    }
    if not claimed:
        return outcome
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log_file:
            subprocess.Popen(
                [sys.executable, str(planner_script), "--dry-run", "--verbose"],
                cwd=str(planner_script.parent),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
    except OSError as exc:
        ev_session.release_replan_claim(session_path)
        outcome["error"] = str(exc)
        return outcome
    outcome["started"] = True
    return outcome


def build_runtime_state(
    *,
    now: datetime,
    cfg: Config,
    forecast_doc: Optional[dict],
    forecast_valid: bool,
    forecast_reasons: list[str],
    current_slot: Optional[dict],
    live_state: dict,
    battery_decision: dict,
    boiler_decision: dict,
    relay_health: dict,
    detected_loads: dict,
    deviation_detected: bool,
    deviation_reason: str,
    boiler_ledger: Optional[dict] = None,
    telemetry_evidence: Optional[dict] = None,
    ev_charging_session: Optional[dict] = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "ts": now.isoformat(),
        "config_hash": cfg.config_hash,
        "forecast": {
            "valid": forecast_valid,
            "reasons": forecast_reasons,
            "generated_at": forecast_doc.get("generated_at") if forecast_doc else None,
            "valid_until": forecast_doc.get("valid_until") if forecast_doc else None,
            "model_version": forecast_doc.get("model_version") if forecast_doc else None,
        },
        "current_slot_start": current_slot.get("slot_start") if current_slot else None,
        "live_state": live_state,
        "battery_decision": battery_decision,
        "boiler_decision": boiler_decision,
        "boiler_control_ledger": boiler_ledger or {},
        "boiler_telemetry": telemetry_evidence or {},
        "relay_health": relay_health,
        "detected_loads": detected_loads,
        "ev_charging_session": ev_charging_session or {},
        "export_limit": {
            "status": "not_owned_by_v10_executor",
            "note": "existing_check_power_out_only",
        },
        "plan_deviation": {
            "detected": deviation_detected,
            "reason": deviation_reason,
        },
        "write_gates": {
            "dry_run": cfg.system.dry_run,
            "battery_write_enabled": cfg.system.battery_write_enabled,
            "boiler_write_enabled": cfg.system.boiler_write_enabled,
        },
    }


def send_executor_alerts(
    *,
    now: datetime,
    cfg: Config,
    forecast_valid: bool,
    forecast_reasons: list[str],
    boiler_decision: dict,
    relay_health: dict,
    detected_loads: dict,
    deviation_detected: bool,
    deviation_reason: str,
    battery_decision: Optional[dict] = None,
    device_failures: Optional[dict] = None,
    actual_soc: Any = None,
    boiler_ledger: Optional[dict] = None,
    ev_charging_session: Optional[dict] = None,
    ev_session_path: Optional[Path] = None,
    alert_state_path: Path = ALERT_STATE_PATH,
) -> list[dict]:
    """Send deduplicated executor-side alerts via notify_admins.sh."""

    outcomes: list[dict] = []
    if not forecast_valid:
        reasons = ", ".join(forecast_reasons) if forecast_reasons else "unknown"
        outcomes.append(alerting.notify_once(
            "executor.forecast_invalid",
            f"FVE ALERT: executor používá fail-safe, forecast není validní: {reasons}",
            cfg=cfg,
            state_path=alert_state_path,
            now=now,
        ))

    unexpected = detected_loads.get("unexpected_load", {}) if isinstance(detected_loads, dict) else {}
    if isinstance(unexpected, dict) and unexpected.get("active"):
        kw = unexpected.get("kw", "n/a")
        phase = unexpected.get("dominant_phase") or "unknown"
        started = unexpected.get("started_at") or "unknown"
        outcomes.append(alerting.notify_once(
            f"executor.unexpected_load.{phase}",
            f"FVE ALERT: neočekávaná zátěž {kw} kW na {phase}, od {started}. Doporučen replan.",
            cfg=cfg,
            state_path=alert_state_path,
            now=now,
        ))

    device_failures = device_failures or {}
    relay_failures = int(device_failures.get("relay", {}).get("consecutive_failures", 0) or 0)
    inverter_failures = int(device_failures.get("inverter", {}).get("consecutive_failures", 0) or 0)
    battery_failures = int(device_failures.get("battery_eco", {}).get("consecutive_failures", 0) or 0)

    if boiler_decision.get("status") == "relay_write_verification_failed" and relay_failures >= 3:
        outcomes.append(alerting.notify_once(
            "executor.relay_write_verification_failed",
            "FVE ALERT: zápis do relé bojleru neprošel read-back verifikací. Zkontroluj relátka.",
            cfg=cfg,
            state_path=alert_state_path,
            now=now,
        ))

    if relay_health.get("relay_status_ok") is False and relay_failures >= 3:
        outcomes.append(alerting.notify_once(
            "executor.relay_status_read_failed",
            "FVE ALERT: executor nedokázal ověřit dostupnost/stav relé bojleru přes /98. Zkontroluj relátka a jejich server.",
            cfg=cfg,
            state_path=alert_state_path,
            now=now,
        ))

    if inverter_failures >= 3:
        outcomes.append(alerting.notify_once(
            "executor.inverter_read_failed_after_3",
            f"FVE ALERT: střídač se neozval ve {inverter_failures} po sobě jdoucích bězích executoru.",
            cfg=cfg, state_path=alert_state_path, now=now,
        ))
    if battery_failures >= 3:
        status = (battery_decision or {}).get("status", "unknown")
        outcomes.append(alerting.notify_once(
            "executor.eco_write_failed_after_3",
            f"FVE ALERT: zápis/ověření ECO plánu selhal ve {battery_failures} po sobě jdoucích bězích (stav {status}).",
            cfg=cfg, state_path=alert_state_path, now=now,
        ))

    # In DRY_RUN/shadow mode v10 intentionally does not control the battery, so
    # SoC can diverge from MILP trajectory. Keep the deviation in state_history,
    # but do not spam admins until real battery writes are enabled.
    soc_deviation_alert_enabled = (not cfg.system.dry_run) and cfg.system.battery_write_enabled
    if deviation_detected and deviation_reason not in ("UNEXPECTED_LOAD_REPLAN",) and soc_deviation_alert_enabled:
        outcomes.append(alerting.notify_once(
            f"executor.plan_deviation.{deviation_reason.split('_')[0] if deviation_reason else 'unknown'}",
            soc_deviation_alert_message(deviation_reason, actual_soc),
            cfg=cfg,
            state_path=alert_state_path,
            now=now,
            deduplicate_message=False,
        ))

    for candidate in completion_notification_candidates(
        now=now,
        ledger=boiler_ledger if isinstance(boiler_ledger, dict) else {},
        session_state=ev_charging_session if isinstance(ev_charging_session, dict) else {},
    ):
        outcome = alerting.notify_once(
            candidate["key"], candidate["message"], cfg=cfg,
            state_path=alert_state_path, now=now, repeat_minutes=10 * 365 * 24 * 60,
        )
        outcomes.append(outcome)
        if outcome.get("sent"):
            if candidate["kind"] == "boiler_full":
                boiler_state.today_entry(boiler_ledger, now.date())["full_notification_sent_at"] = now.isoformat()
            elif candidate["kind"] == "ev_closed":
                ev_charging_session["completion_notification_sent_at"] = now.isoformat()
                if ev_session_path is not None:
                    ev_session.mark_completion_notification_sent(
                        ev_session_path, session_id=str(ev_charging_session["session_id"]), now=now,
                    )

    return outcomes


async def read_live_state_or_fail() -> dict:
    live = await planner_module.read_live_state()
    if live.get("battery_soc") is None:
        raise RuntimeError("live_state neobsahuje battery_soc")
    return live


def run_executor(
    *,
    cfg: Config,
    now: datetime,
    live_state: dict,
    forecast_path: Path = FORECAST_PATH,
    runtime_path: Path = RUNTIME_STATE_PATH,
    history_path: Path = STATE_HISTORY_PATH,
    boiler_state_path: Path = BOILER_CONTROL_STATE_PATH,
    ev_session_path: Path = EV_SESSION_STATE_PATH,
    requests_path: Path = REQUESTS_PATH,
    reports_dir: str = telemetry.paths.GOODWE_REPORTS_DIR,
    verbose: bool = True,
) -> dict:
    forecast_doc = load_forecast(forecast_path)
    forecast_valid, forecast_reasons = validate_forecast(forecast_doc, cfg, now)
    current_slot = find_current_slot(forecast_doc.get("slots", []) if forecast_doc else [], now, cfg)
    if forecast_valid and current_slot is None:
        forecast_valid = False
        forecast_reasons = [*forecast_reasons, "FORECAST_CURRENT_SLOT_MISSING"]

    battery_decision = decide_battery_execution(forecast_doc, current_slot, cfg, forecast_valid, now)
    relay_health = check_relay_health()
    ledger = boiler_state.normalize_state(read_json(boiler_state_path, {}))
    readback_mask = relay_mask_from_health(relay_health)
    if relay_health.get("relay_status_ok") is True:
        accounting_mask = readback_mask
        accounting_mask_source = "relay_readback"
    else:
        accounting_mask = tuple(bool(value) for value in ledger.get("current_mask", [False, False, False]))
        accounting_mask_source = "last_confirmed_ledger_mask_relay_unavailable"
    previous_at = _parse_optional_datetime(ledger.get("previous_executor_at"), ZoneInfo(cfg.system.timezone))
    samples = telemetry.load_recent_samples(
        now=now, live_state=live_state, since=previous_at, reports_dir=reports_dir, lookback_minutes=6.0,
    )
    ledger = boiler_state.update_energy_and_baselines(
        ledger, now=now, samples=samples, relay_mask=accounting_mask, phase_power_kw=cfg.boiler.phase_power_kw,
    )
    ledger["accounting_mask_source"] = accounting_mask_source
    telemetry_evidence = telemetry.robust_evidence(
        samples, accounting_mask, cfg.boiler.phase_power_kw, now=now,
        persisted_phase_baseline_kw=ledger.get("phase_baseline_kw"),
    )
    boiler_completion = detect_boiler_full_completion(ledger, telemetry_evidence, now=now)
    ledger["boiler_full_completion"] = boiler_completion
    battery_failure = update_device_failure_counter(
        "battery_eco", battery_decision.get("status") not in ("eco_write_failed", "eco_write_verification_failed"), now=now,
    )
    boiler_decision = decide_boiler_execution(
        current_slot,
        live_state,
        cfg,
        forecast_valid,
        relay_health=relay_health,
        forecast_doc=forecast_doc,
        now=now,
        telemetry_evidence=telemetry_evidence,
        ledger=ledger,
    )
    relay_cycle_healthy = (
        relay_health.get("relay_status_ok") is True
        and boiler_decision.get("status") != "relay_write_verification_failed"
    )
    relay_failure = update_device_failure_counter("relay", relay_cycle_healthy, now=now)
    confirmed_post_mask = accounting_mask
    confirmed_mask_source = accounting_mask_source
    adapter_result = boiler_decision.get("adapter_result", {})
    adapter_after_mask = adapter_result.get("after_mask") if isinstance(adapter_result, dict) else None
    if isinstance(adapter_after_mask, list) and len(adapter_after_mask) == 3 and all(isinstance(value, bool) for value in adapter_after_mask):
        confirmed_post_mask = tuple(adapter_after_mask)
        confirmed_mask_source = "relay_adapter_final_readback"
    elif boiler_decision.get("status") == "relay_written":
        target_mask = boiler_decision.get("target_mask")
        if isinstance(target_mask, list) and len(target_mask) == 3:
            confirmed_post_mask = tuple(bool(value) for value in target_mask)
            confirmed_mask_source = "verified_target"
    ledger = boiler_state.record_mask_transition(
        ledger, old_mask=accounting_mask, new_mask=confirmed_post_mask, now=now,
    )
    ledger["current_mask_source"] = confirmed_mask_source
    atomic_write_json(boiler_state_path, ledger)
    wallbox_state = read_wallbox_state_for_detection()
    session_state = ev_session.update_persisted_session(
        ev_session_path,
        now=now,
        wallbox=wallbox_state,
        active_ev_request=active_ev_request(now, requests_path),
    )
    session_replan = trigger_ev_session_replan(now=now, session_path=ev_session_path)
    detected_loads = detect_runtime_loads(
        now=now,
        cfg=cfg,
        live_state=live_state,
        current_slot=current_slot,
        boiler_ledger=ledger,
        telemetry_evidence=telemetry_evidence,
        history_path=history_path,
        detector_path=DETECTED_LOADS_PATH,
        wallbox_state=wallbox_state,
    )
    deviation_detected, deviation_reason = detect_plan_deviation(current_slot, live_state, cfg, now=now)
    if detected_loads.get("unexpected_load", {}).get("replan_recommended"):
        deviation_detected = True
        deviation_reason = "UNEXPECTED_LOAD_REPLAN"

    runtime = build_runtime_state(
        now=now,
        cfg=cfg,
        forecast_doc=forecast_doc,
        forecast_valid=forecast_valid,
        forecast_reasons=forecast_reasons,
        current_slot=current_slot,
        live_state=live_state,
        battery_decision=battery_decision,
        boiler_decision=boiler_decision,
        relay_health=relay_health,
        detected_loads=detected_loads,
        deviation_detected=deviation_detected,
        deviation_reason=deviation_reason,
        boiler_ledger=ledger,
        telemetry_evidence=telemetry_evidence,
        ev_charging_session=session_state,
    )
    runtime["ev_session_replan"] = session_replan
    runtime["device_failures"] = {"relay": relay_failure, "battery_eco": battery_failure}
    runtime["alerts"] = send_executor_alerts(
        now=now,
        cfg=cfg,
        forecast_valid=forecast_valid,
        forecast_reasons=forecast_reasons,
        boiler_decision=boiler_decision,
        relay_health=relay_health,
        battery_decision=battery_decision,
        device_failures=runtime["device_failures"],
        detected_loads=detected_loads,
        deviation_detected=deviation_detected,
        deviation_reason=deviation_reason,
        actual_soc=live_state.get("battery_soc"),
        boiler_ledger=ledger,
        ev_charging_session=session_state,
        ev_session_path=ev_session_path,
    )
    atomic_write_json(boiler_state_path, ledger)
    atomic_write_json(DETECTED_LOADS_PATH, detected_loads)
    atomic_write_json(runtime_path, runtime)
    append_jsonl(history_path, runtime)
    log(
        f"runtime_state zapsán, forecast_valid={forecast_valid}, "
        f"battery={battery_decision['status']}, boiler={boiler_decision['status']}",
        verbose=verbose,
    )
    return runtime


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="executor.py v11 production tactical layer")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Cesta ke config.toml")
    parser.add_argument("--forecast", default=str(FORECAST_PATH), help="Cesta k forecast_48h.json")
    parser.add_argument("--runtime", default=str(RUNTIME_STATE_PATH), help="Výstupní runtime_state.json")
    parser.add_argument("--history", default=str(STATE_HISTORY_PATH), help="Append-only state_history.jsonl")
    parser.add_argument("--initial-soc", type=float, default=None, help="Testovací override SoC; přeskočí live GoodWe read")
    parser.add_argument("--verbose", action="store_true", help="Verbose log na stdout")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    now = datetime.now(ZoneInfo(cfg.system.timezone))
    log(f"=== executor.py v11 start (version={VERSION}, dry_run={cfg.system.dry_run}) ===", verbose=args.verbose)

    if args.initial_soc is not None:
        live_state = {
            "inverter_reachable": False,
            "battery_soc": args.initial_soc,
            "source": "--initial-soc",
        }
    else:
        try:
            live_state = asyncio.run(read_live_state_or_fail())
        except Exception as e:
            print(f"CHYBA: nelze přečíst live stav střídače: {e}", file=sys.stderr)
            failure = update_device_failure_counter("inverter", False, now=now)
            if failure["consecutive_failures"] >= 3:
                alerting.notify_once(
                    "executor.inverter_read_failed_after_3",
                    f"FVE ALERT: střídač se neozval ve {failure['consecutive_failures']} po sobě jdoucích bězích executoru: {e}",
                    cfg=cfg,
                    state_path=ALERT_STATE_PATH,
                    now=now,
                )
            return 3

    update_device_failure_counter("inverter", True, now=now)

    run_executor(
        cfg=cfg,
        now=now,
        live_state=live_state,
        forecast_path=Path(args.forecast),
        runtime_path=Path(args.runtime),
        history_path=Path(args.history),
        verbose=args.verbose,
    )
    log("=== executor.py v11 konec ===", verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
