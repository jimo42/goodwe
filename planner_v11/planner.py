#!/usr/bin/env python3
"""
planner.py v11 - stateless produkční orchestrace 48h MILP plánu.

Autoritativní zdroje:
  - ARCHITECTURE_DESIGN_v10_FINAL.md sekce 2.1 (strategická vrstva),
    6.x (ceny/PV/terminální hodnota), 7.x (zátěže), 8.x (požadavky),
    10.x (MILP) a 15.1 (`forecast_48h.json`).
  - SERVER_IMPLEMENTATION_GUIDE_v10.md sekce 11/15/16.

Rozsah této v11 verze:
  - ŽÁDNÉ zápisy do střídače, ECO režimů ani relé. Planner pouze čte vstupy,
    zavolá `lib.optimizer.optimize()` a atomicky zapíše JSON plán.
  - Live SoC se čte read-only přes existující GoodWe knihovnu podle ověřeného
    vzoru ze starého `planner/lib/inverter_client.py` (read_runtime_data).
  - Pokud nejsou zatím k dispozici noční profilační soubory, base-load a pool
    heat-pump část používá explicitní konzervativní fallback zdokumentovaný v
    `lib/load_model.py`; pevná bazénová cirkulace se vkládá z configu vždy.
  - EV/boiler requesty se čtou z `state/requests.json`, pokud existuje. Formát
    je robustně tolerován jako list nebo objekt s klíčem `requests`.

Vědomá zjednodušení pro v1:
  - Neexistuje zatím `update_base_load_profile.py`; `state/base_load_profile.json`
    je volitelný a při absenci se používá fallback 0.46 kW.
  - Pool heat-pump profil není bez state souboru predikován (0 kWh + evidence).
  - Neprobíhá příprava/zápis near-term ECO akcí; to přijde až s executorem a
    adaptéry s read-back verifikací.

VERSION = "1.6"

Changelog:
- v1.6 (2026-08-07): Consume the persistent EV session ledger, reserve active
  physical charging to 9 kWh, lock ongoing windows and avoid detector double count.
- v1.5 (2026-08-04): Notify significant EV recommendation start shifts
  against the last successfully announced request-scoped baseline.
- v1.4 (2026-08-02): Production v11 release paired with the v11 executor;
  the planner remains read-only with respect to devices.
- v1.3 (2026-07-27): Mark past active requests as expired before using them
  for a new forecast.
- v1.2 (2026-07-25): Add detector observability fields, planner duration,
  unannounced EV fixed-load assumption, and split boiler plan into hard request
  vs economically opportunistic heating.
- v1.1 (2026-07-24): Send deduplicated notify_admins alerts for inverter read
  failures, solver non-optimal status and physically infeasible user requests.
"""
from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from lib import alerting, boiler_model, boiler_state, economics, ev_model, ev_session, load_model, optimizer, paths, pool_model, prices, request_store, weather
from lib.config import Config, ConfigError, load_config


VERSION = "1.6"
MODEL_VERSION = "11-planner-v1"
SCHEMA_VERSION = 10
EV_SCHEDULE_CHANGE_MINUTES = 60.0

PLANNER_DIR = Path(__file__).resolve().parent
STATE_DIR = PLANNER_DIR / "state"
DEFAULT_CONFIG_PATH = PLANNER_DIR / "config.toml"
DEFAULT_FORECAST_PATH = STATE_DIR / "forecast_48h.json"
REQUESTS_PATH = STATE_DIR / "requests.json"
BASE_LOAD_PROFILE_PATH = STATE_DIR / "base_load_profile.json"
DETECTED_LOADS_PATH = STATE_DIR / "detected_loads.json"
ALERT_STATE_PATH = STATE_DIR / "alert_state.json"
BOILER_CONTROL_STATE_PATH = STATE_DIR / "boiler_control_state.json"
EV_SESSION_STATE_PATH = STATE_DIR / "ev_session_state.json"

GOODWE_LIB_DIR = os.path.join(paths.BASE_DIR, "goodwe", "goodwe")
GOODWE_CONF_PATH = os.path.join(paths.BASE_DIR, "conf", "goodwe.conf")

# CONTROL_LOGIC_SPEC_v10.yaml `base_load.fallback_summer_overnight_reserve_kw`.
FALLBACK_SUMMER_OVERNIGHT_RESERVE_KW = 0.46


@dataclass(frozen=True)
class SlotPlanInput:
    """Diagnostická metadata k jednomu optimizer SlotInput pro JSON výstup."""

    slot_start: datetime
    price_eur_mwh: float
    price_export_eur_mwh: float
    price_source: str
    import_price_czk_kwh: float
    export_revenue_czk_kwh: float
    pv_estimate_kwh: float
    base_load_expected_kwh: float
    base_load_reserve_kwh: float
    base_load_source: str
    pool_load_kwh: float
    pool_heat_pump_kwh: float
    additional_load_kwh: float
    export_allowed: bool
    effective_import_nonpositive: bool
    sun_pct: Optional[float]
    cloudcover_pct: Optional[float]

    @property
    def fixed_load_kwh(self) -> float:
        return (
            self.base_load_expected_kwh
            + self.pool_load_kwh
            + self.pool_heat_pump_kwh
            + self.additional_load_kwh
        )


def log(message: str, *, verbose: bool = True) -> None:
    if verbose:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}")


def round_down_to_slot(dt: datetime, slot_minutes: int) -> datetime:
    minute = (dt.minute // slot_minutes) * slot_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def slot_starts(now: datetime, cfg: Config) -> list[datetime]:
    start = round_down_to_slot(now, cfg.system.planning_step_minutes)
    return [
        start + timedelta(minutes=cfg.system.planning_step_minutes * i)
        for i in range(cfg.system.horizon_slots)
    ]


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def parse_iso_datetime(value: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _same_tz(dt: datetime, reference: datetime, fallback_tz: ZoneInfo) -> datetime:
    tz = reference.tzinfo or fallback_tz
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def load_base_load_profile(path: Path = BASE_LOAD_PROFILE_PATH) -> dict[str, load_model.SlotProfile]:
    """Načte volitelný `state/base_load_profile.json`.

    Tolerovaný formát: `{slot_key: {expected_kw, reserve_kw, sample_count}}` nebo
    objekt s top-level klíčem `profile` ve stejném tvaru. Chybný/chybějící soubor
    znamená prázdný profil a fallback v `load_model.expected_load_kwh_for_slot`.
    """
    raw = read_json(path, {})
    if isinstance(raw, dict) and isinstance(raw.get("profile"), dict):
        raw = raw["profile"]
    if not isinstance(raw, dict):
        return {}

    profile: dict[str, load_model.SlotProfile] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        try:
            profile[str(key)] = load_model.SlotProfile(
                expected_kw=float(value["expected_kw"]),
                reserve_kw=float(value["reserve_kw"]),
                sample_count=int(value.get("sample_count", 0)),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return profile


def load_additional_load_requests(path: Path, tz: ZoneInfo) -> list[dict]:
    """Load active user-announced uncontrollable loads from requests.json."""
    raw = read_json(path, [])
    if isinstance(raw, dict):
        raw = raw.get("requests", [])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("type") != "additional_load":
            continue
        if item.get("status", "active") != "active":
            continue
        try:
            start = parse_iso_datetime(str(item["start"]), tz)
            end = parse_iso_datetime(str(item["end"]), tz)
            power_kw = float(item["power_kw"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start or power_kw <= 0:
            continue
        out.append({
            "id": item.get("id"),
            "start": start,
            "end": end,
            "power_kw": power_kw,
            "phase": item.get("phase"),
            "description": item.get("description"),
        })
    return out


def additional_load_kwh_for_slot(
    dt: datetime,
    slot_minutes: int,
    additional_requests: list[dict],
    detected_loads: Optional[dict],
) -> float:
    """Return announced + temporarily detected uncontrollable load for a slot."""
    slot_hours = slot_minutes / 60.0
    total_kw = 0.0
    for req in additional_requests:
        if req["start"] <= dt < req["end"]:
            total_kw += float(req["power_kw"])

    if isinstance(detected_loads, dict):
        adj = detected_loads.get("planning_adjustment", {})
        try:
            adj_kw = float(adj.get("kw", 0.0) or 0.0)
            valid_until_raw = adj.get("valid_until")
            valid_until = parse_iso_datetime(valid_until_raw, dt.tzinfo or ZoneInfo("Europe/Prague")) if valid_until_raw else None
        except (TypeError, ValueError):
            adj_kw = 0.0
            valid_until = None
        if adj_kw > 0 and valid_until is not None and _same_tz(dt, valid_until, ZoneInfo("Europe/Prague")) < valid_until:
            total_kw += adj_kw

        unannounced_ev = detected_loads.get("unannounced_ev_load", {})
        if isinstance(unannounced_ev, dict) and unannounced_ev.get("active"):
            try:
                ev_kw = float(unannounced_ev.get("power_kw", 0.0) or 0.0)
                ev_valid_until_raw = unannounced_ev.get("valid_until")
                ev_valid_until = parse_iso_datetime(ev_valid_until_raw, dt.tzinfo or ZoneInfo("Europe/Prague")) if ev_valid_until_raw else None
            except (TypeError, ValueError):
                ev_kw = 0.0
                ev_valid_until = None
            if ev_kw > 0 and ev_valid_until is not None and _same_tz(dt, ev_valid_until, ZoneInfo("Europe/Prague")) < ev_valid_until:
                total_kw += ev_kw

    return total_kw * slot_hours


def additional_load_breakdown_for_slot(
    dt: datetime,
    slot_minutes: int,
    additional_requests: list[dict],
    detected_loads: Optional[dict],
) -> dict:
    """Diagnostic split of additional load sources for forecast observability."""
    slot_hours = slot_minutes / 60.0
    announced_kw = 0.0
    for req in additional_requests:
        if req["start"] <= dt < req["end"]:
            announced_kw += float(req["power_kw"])

    detector_kw = 0.0
    unannounced_ev_kw = 0.0
    if isinstance(detected_loads, dict):
        adj = detected_loads.get("planning_adjustment", {})
        try:
            adj_kw = float(adj.get("kw", 0.0) or 0.0)
            valid_until_raw = adj.get("valid_until")
            valid_until = parse_iso_datetime(valid_until_raw, dt.tzinfo or ZoneInfo("Europe/Prague")) if valid_until_raw else None
        except (TypeError, ValueError):
            adj_kw = 0.0
            valid_until = None
        if adj_kw > 0 and valid_until is not None and _same_tz(dt, valid_until, ZoneInfo("Europe/Prague")) < valid_until:
            detector_kw = adj_kw

        unannounced_ev = detected_loads.get("unannounced_ev_load", {})
        if isinstance(unannounced_ev, dict) and unannounced_ev.get("active"):
            try:
                ev_kw = float(unannounced_ev.get("power_kw", 0.0) or 0.0)
                ev_valid_until_raw = unannounced_ev.get("valid_until")
                ev_valid_until = parse_iso_datetime(ev_valid_until_raw, dt.tzinfo or ZoneInfo("Europe/Prague")) if ev_valid_until_raw else None
            except (TypeError, ValueError):
                ev_kw = 0.0
                ev_valid_until = None
            if ev_kw > 0 and ev_valid_until is not None and _same_tz(dt, ev_valid_until, ZoneInfo("Europe/Prague")) < ev_valid_until:
                unannounced_ev_kw = ev_kw

    return {
        "announced_kw": round(announced_kw, 6),
        "detector_adjustment_kw": round(detector_kw, 6),
        "unannounced_ev_kw": round(unannounced_ev_kw, 6),
        "announced_kwh": round(announced_kw * slot_hours, 6),
        "detector_adjustment_kwh": round(detector_kw * slot_hours, 6),
        "unannounced_ev_kwh": round(unannounced_ev_kw * slot_hours, 6),
    }


def price_inputs_for_slot(
    dt: datetime,
    cfg: Config,
    median_cache: dict,
    prices_dir: str = paths.PRICES_DIR,
) -> tuple[float, float, str, float, float, bool, bool]:
    """Vrátí cenové vstupy pro optimizer i JSON.

    Actual cena se použije pro import i export. Pokud chybí, použije se
    median-based fallback z `lib.prices`: import estimate je konzervativně
    vyšší, export estimate konzervativně nižší.
    """
    actual = prices.get_actual_price_for_slot(dt.replace(tzinfo=None), prices_dir=prices_dir)
    if actual is not None:
        import_spot = export_spot = actual
        source = "actual"
    else:
        before = dt.date()
        if before not in median_cache:
            median_cache[before] = prices.historical_median_eur_mwh(before, prices_dir=prices_dir)
        median = median_cache[before]
        if median is None:
            median = 0.0
            source = "estimated_no_history"
        else:
            source = "estimated_median_fallback"
        import_spot, export_spot = prices.fallback_estimate_eur_mwh(median, cfg)

    import_czk = economics.import_cost_czk_per_kwh(import_spot, cfg)
    export_czk = economics.export_revenue_czk_per_kwh(export_spot, cfg)
    export_allowed = economics.is_export_allowed(export_spot, cfg)
    effective_import_nonpositive = economics.is_effective_import_nonpositive(import_spot, cfg)
    return import_spot, export_spot, source, import_czk, export_czk, export_allowed, effective_import_nonpositive


def build_plan_inputs(
    starts: list[datetime],
    cfg: Config,
    *,
    prices_dir: str = paths.PRICES_DIR,
    weather_dir: str = paths.WEATHER_DIR,
    base_profile: Optional[dict[str, load_model.SlotProfile]] = None,
    additional_requests: Optional[list[dict]] = None,
    detected_loads: Optional[dict] = None,
) -> tuple[list[optimizer.SlotInput], list[SlotPlanInput]]:
    base_profile = base_profile if base_profile is not None else load_base_load_profile()
    additional_requests = additional_requests if additional_requests is not None else []
    detected_loads = detected_loads if isinstance(detected_loads, dict) else {}
    median_cache: dict = {}
    opt_slots: list[optimizer.SlotInput] = []
    meta: list[SlotPlanInput] = []

    for dt in starts:
        naive_dt = dt.replace(tzinfo=None)
        (
            import_spot,
            export_spot,
            price_source,
            import_czk,
            export_czk,
            export_allowed,
            effective_import_nonpositive,
        ) = price_inputs_for_slot(naive_dt, cfg, median_cache, prices_dir)

        pv_kwh = weather.pv_estimate_kwh_for_slot(
            naive_dt, cfg.system.planning_step_minutes, weather_dir=weather_dir
        )
        base_expected, base_reserve, base_source = load_model.expected_load_kwh_for_slot(
            base_profile,
            naive_dt,
            cfg.system.planning_step_minutes,
            FALLBACK_SUMMER_OVERNIGHT_RESERVE_KW,
        )
        pool_kwh = pool_model.circulation_load_kwh_for_slot(
            naive_dt, cfg.system.planning_step_minutes, cfg
        )
        pool_heat_pump_kwh = 0.0
        additional_load_kwh = additional_load_kwh_for_slot(
            dt,
            cfg.system.planning_step_minutes,
            additional_requests,
            detected_loads,
        )

        m = SlotPlanInput(
            slot_start=dt,
            price_eur_mwh=import_spot,
            price_export_eur_mwh=export_spot,
            price_source=price_source,
            import_price_czk_kwh=import_czk,
            export_revenue_czk_kwh=export_czk,
            pv_estimate_kwh=pv_kwh,
            base_load_expected_kwh=base_expected,
            base_load_reserve_kwh=base_reserve,
            base_load_source=base_source,
            pool_load_kwh=pool_kwh,
            pool_heat_pump_kwh=pool_heat_pump_kwh,
            additional_load_kwh=additional_load_kwh,
            export_allowed=export_allowed,
            effective_import_nonpositive=effective_import_nonpositive,
            sun_pct=weather.sun_pct_for_slot(naive_dt, weather_dir=weather_dir),
            cloudcover_pct=weather.cloudcover_pct_for_slot(naive_dt, weather_dir=weather_dir),
        )
        meta.append(m)
        opt_slots.append(
            optimizer.SlotInput(
                slot_start=dt,
                price_import_czk_kwh=import_czk,
                price_export_czk_kwh=export_czk,
                export_allowed=export_allowed,
                effective_import_nonpositive=effective_import_nonpositive,
                pv_kwh=pv_kwh,
                fixed_load_kwh=m.fixed_load_kwh,
            )
        )

    return opt_slots, meta


def compute_terminal_value_czk_per_kwh(meta: list[SlotPlanInput], cfg: Config) -> float:
    """Konzervativní terminální hodnota baterie na konci horizontu.

    ARCH 6.4/HANDOFF past: hodnota nesmí být záporná ani optimističtější než
    realistický import posledního slotu. Proto použijeme nejlepší importní cenu
    v posledních `terminal_value_lookahead_hours` hodinách horizontu mínus
    budoucí bateriový cyklový náklad a výsledek omezíme shora importní cenou
    posledního slotu.
    """
    if not meta:
        return 0.0
    slots_count = max(1, int(cfg.battery.terminal_value_lookahead_hours * 60 / cfg.system.planning_step_minutes))
    tail = meta[-slots_count:]
    best_future_import = max(s.import_price_czk_kwh for s in tail)
    cycle_cost = economics.battery_cycle_cost_czk_per_kwh(cfg)
    candidate = max(0.0, best_future_import - cycle_cost)
    last_slot_import = max(0.0, meta[-1].import_price_czk_kwh)
    return min(candidate, last_slot_import)


def load_active_requests(path: Path = REQUESTS_PATH, tz: Optional[ZoneInfo] = None) -> list[dict]:
    tz = tz or ZoneInfo("Europe/Prague")
    raw = read_json(path, [])
    if isinstance(raw, dict):
        raw = raw.get("requests", [])
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict) or item.get("status", "active") != "active":
            continue
        normalized = dict(item)
        for key in ("created_at", "available_from", "deadline"):
            if isinstance(normalized.get(key), str):
                try:
                    normalized[key] = parse_iso_datetime(normalized[key], tz)
                except ValueError:
                    normalized[key] = None
        out.append(normalized)
    return out


def detected_loads_without_session_ev(detected_loads: Any, session_state: Any) -> dict:
    """Remove only EV projections when ACTIVE/PAUSED is modeled as EvRequest."""

    source = detected_loads if isinstance(detected_loads, dict) else {}
    cleaned = dict(source)
    if not isinstance(session_state, dict) or session_state.get("state") not in ev_session.ACTIVE_STATES:
        return cleaned
    unannounced = cleaned.get("unannounced_ev_load")
    if isinstance(unannounced, dict):
        cleaned["unannounced_ev_load"] = {
            **unannounced,
            "active": False,
            "power_kw": 0.0,
            "assumed_total_kwh": 0.0,
            "reason": "MODELED_BY_PERSISTENT_EV_SESSION",
        }
    adjustment = cleaned.get("planning_adjustment")
    if isinstance(adjustment, dict):
        components = adjustment.get("components_kw")
        if isinstance(components, dict):
            ev_kw = max(0.0, float(components.get("ev", 0.0) or 0.0))
            remaining = max(0.0, float(adjustment.get("kw", 0.0) or 0.0) - ev_kw)
            cleaned["planning_adjustment"] = {
                **adjustment,
                "kw": round(remaining, 3),
                "components_kw": {**components, "ev": 0.0},
            }
    return cleaned


def choose_requests(
    requests_list: list[dict],
    starts: list[datetime],
    cfg: Config,
    opt_slots: list[optimizer.SlotInput],
    initial_soc_kwh: float,
    terminal_value_czk_per_kwh: float,
    boiler_daily_limits: Optional[dict] = None,
    ev_session_state: Optional[dict] = None,
) -> tuple[Optional[optimizer.EvRequest], Optional[optimizer.BoilerHardRequest], list[dict]]:
    """Vybere podporované aktivní požadavky a vrátí optimizer requesty + JSON summary."""
    active_summary: list[dict] = []
    ev_source_req: Optional[dict] = None
    ev_req: Optional[optimizer.EvRequest] = None
    boiler_req: Optional[optimizer.BoilerHardRequest] = None
    step = timedelta(minutes=cfg.system.planning_step_minutes)
    horizon_end = starts[-1] + step if starts else None

    # První průchod: nejdřív sestavit boiler hard request. EV kandidáti se pak
    # hodnotí už proti finálnímu kontextu včetně bojler deadline, nezávisle na
    # pořadí záznamů v requests.json.
    for req in requests_list:
        rtype = req.get("type")
        if rtype == "boiler_full" and boiler_req is None:
            deadline = req.get("deadline")
            if not isinstance(deadline, datetime):
                active_summary.append({"type": rtype, "status": "invalid_datetime"})
                continue
            already_delivered = float(req.get("already_delivered_kwh", 0.0) or 0.0)
            boiler_req = boiler_model.build_hard_request(starts, deadline, already_delivered, cfg)
            active_summary.append({
                "type": rtype,
                "id": req.get("id") or req.get("request_id"),
                "deadline": deadline.isoformat(),
                "already_delivered_kwh": already_delivered,
                "optimizer_request": asdict(boiler_req) if boiler_req else None,
            })
        elif rtype == "ev_charge" and ev_source_req is None:
            ev_source_req = req
        elif rtype in ("boiler_full", "ev_charge"):
            active_summary.append({"type": rtype, "status": "unsupported_or_duplicate"})
        elif rtype == "additional_load":
            active_summary.append({
                "type": rtype,
                "status": "included_as_fixed_load_if_active",
                "id": req.get("id"),
                "power_kw": req.get("power_kw"),
                "start": req.get("start").isoformat() if isinstance(req.get("start"), datetime) else req.get("start"),
                "end": req.get("end").isoformat() if isinstance(req.get("end"), datetime) else req.get("end"),
            })
        else:
            active_summary.append({"type": rtype or "unknown", "status": "unsupported_or_duplicate"})

    session = ev_session_state if isinstance(ev_session_state, dict) else {}
    session_status = str(session.get("state") or "IDLE")
    session_request_id = str(session.get("request_id") or "")
    if session_status in ev_session.ACTIVE_STATES and starts:
        planning_remaining = max(0.0, min(
            ev_session.MAX_SESSION_KWH,
            float(session.get("physical_remaining_to_max_kwh", 0.0) or 0.0),
        ))
        current_power_kw = max(0.0, float(session.get("current_power_w", 0.0) or 0.0) / 1000.0)
        planning_power_kw = min(
            float(cfg.ev.nominal_power_kw),
            max(float(cfg.ev.planning_power_kw), current_power_kw),
        )
        end_idx = 0
        if planning_remaining > 1e-6:
            needed = ev_model.slots_needed(
                planning_remaining,
                planning_power_kw,
                cfg.system.planning_step_minutes / 60.0,
            )
            end_idx = min(len(starts) - 1, needed - 1)
            ev_req = optimizer.EvRequest(
                0, end_idx, planning_remaining, planning_power_kw, fixed_profile=True
            )
        matching_user = (
            ev_source_req
            if ev_source_req is not None
            and str(ev_source_req.get("id") or ev_source_req.get("request_id") or "") == session_request_id
            else None
        )
        summary_id = session_request_id
        summary_source = session.get("request_source") or "synthetic"
        deadline = matching_user.get("deadline") if matching_user else None
        active_summary.append({
            "type": "ev_charge",
            "id": summary_id,
            "request_source": summary_source,
            "session_id": session.get("session_id"),
            "session_status": session_status,
            "window_locked": True,
            "requested_ac_kwh_original": session.get("requested_ac_kwh_original"),
            "required_ac_kwh": session.get("effective_target_kwh"),
            "delivered_kwh": session.get("delivered_kwh"),
            "request_remaining_kwh": session.get("request_remaining_kwh"),
            "planning_remaining_to_physical_max_kwh": round(planning_remaining, 3),
            "deadline": deadline.isoformat() if isinstance(deadline, datetime) else None,
            "recommendation": {
                "feasible": True,
                "recommended_start": starts[0].isoformat(),
                "latest_safe_start": starts[0].isoformat(),
                "expected_end": (starts[end_idx] + step).isoformat(),
                "expected_delivered_kwh": round(planning_remaining, 3),
                "reason": "Probíhající fyzická relace je zamknutá od aktuálního slotu do maxima 9 kWh.",
            },
        })
        return ev_req, boiler_req, active_summary

    # Druhý průchod: EV doporučení a optimizer EvRequest s boiler_req už známým.
    if ev_source_req is not None:
        req = ev_source_req
        rtype = req.get("type")
        try:
            original_required = float(req.get("requested_ac_kwh_original", req["required_ac_kwh"]))
            effective_target = min(ev_session.MAX_SESSION_KWH, max(0.0, float(req["required_ac_kwh"])))
            delivered = 0.0
            if session_status == "CLOSED" and session_request_id == str(req.get("id") or req.get("request_id") or ""):
                delivered = max(0.0, float(session.get("delivered_kwh", 0.0) or 0.0))
            raw_remaining = round(max(0.0, effective_target - delivered), 3)
            closure_tolerated = (
                session_status == "CLOSED"
                and session_request_id == str(req.get("id") or req.get("request_id") or "")
                and raw_remaining < ev_session.REPLAN_DEVIATION_KWH
            )
            required = 0.0 if closure_tolerated else raw_remaining
            deadline = req["deadline"]
            available_from = req.get("available_from") or starts[0]
        except (KeyError, TypeError, ValueError):
            active_summary.append({"type": rtype, "status": "invalid"})
        else:
            if not isinstance(deadline, datetime) or not isinstance(available_from, datetime):
                active_summary.append({"type": rtype, "status": "invalid_datetime"})
            elif required <= 1e-6:
                active_summary.append({
                    "type": rtype,
                    "id": req.get("id") or req.get("request_id"),
                    "requested_ac_kwh_original": original_required,
                    "required_ac_kwh": effective_target,
                    "delivered_kwh": delivered,
                    "request_remaining_kwh": 0.0,
                    "actual_request_shortfall_kwh": raw_remaining,
                    "closure_shortfall_tolerated": closure_tolerated,
                    "session_status": session_status if session_request_id else None,
                    "recommendation": {
                        "feasible": True,
                        "recommended_start": None,
                        "latest_safe_start": None,
                        "expected_end": None,
                        "expected_delivered_kwh": 0.0,
                        "reason": "Požadovaný cíl již byl v uzavřené relaci dosažen.",
                    },
                })
            else:
                def evaluate(candidate_req: optimizer.EvRequest) -> optimizer.OptimizerResult:
                    return optimizer.optimize(
                        opt_slots,
                        cfg,
                        initial_soc_kwh=initial_soc_kwh,
                        terminal_value_czk_per_kwh=terminal_value_czk_per_kwh,
                        ev_request=candidate_req,
                        boiler_hard_request=boiler_req,
                        boiler_opportunistic_daily_limits_kwh=boiler_daily_limits,
                    )

                safe_start = ev_model.latest_safe_start(deadline, required, cfg)
                long_horizon_deferrable = (
                    horizon_end is not None and deadline > horizon_end and safe_start > horizon_end
                )
                rec = None
                chosen_start = None
                chosen_end = None
                chosen_feasible = False
                chosen_reason = ""
                expected_delivered = 0.0

                if long_horizon_deferrable:
                    candidates = ev_model.enumerate_candidates(
                        starts,
                        available_from,
                        deadline,
                        required,
                        cfg,
                        [s.price_import_czk_kwh for s in opt_slots],
                    )
                    pv_rich_candidates = []
                    for candidate in candidates:
                        pv_kwh = sum(s.pv_kwh for s in opt_slots[candidate.start_idx : candidate.end_idx + 1])
                        if pv_kwh >= required * 0.5:
                            pv_rich_candidates.append(candidate)
                    if pv_rich_candidates:
                        best_candidate, best_result = ev_model.evaluate_candidates(
                            pv_rich_candidates,
                            required,
                            cfg,
                            evaluate,
                        )
                        if best_candidate is not None and best_result is not None:
                            chosen_start = best_candidate.start_time
                            chosen_end = best_candidate.end_time
                            chosen_feasible = True
                            expected_delivered = required
                            chosen_reason = (
                                "Deadline je mimo aktuální 48h horizont, ale v horizontu je "
                                "PV-rich kandidát (odhad PV >= 50 % požadované energie); "
                                "doporučen ověřený proveditelný interval."
                            )
                        else:
                            chosen_reason = (
                                "Deadline je mimo aktuální 48h horizont; PV-rich kandidáti "
                                "existují, ale žádný z ověřených nebyl plně proveditelný, "
                                "požadavek zůstává aktivní pro další běh planneru."
                            )
                    else:
                        chosen_reason = (
                            "Deadline je mimo aktuální 48h horizont a nejpozdější bezpečný "
                            "start je také mimo horizont; v aktuálním horizontu není PV-rich "
                            "kandidát, požadavek zůstává aktivní pro další běh planneru."
                        )
                else:
                    rec = ev_model.recommend(
                        starts,
                        available_from,
                        deadline,
                        required,
                        cfg,
                        [s.price_import_czk_kwh for s in opt_slots],
                        evaluate,
                    )
                    chosen_start = rec.recommended_start
                    chosen_end = rec.expected_end
                    chosen_feasible = rec.feasible
                    expected_delivered = rec.expected_delivered_kwh
                    chosen_reason = rec.reason

                if chosen_start is not None and chosen_end is not None:
                    try:
                        start_idx = starts.index(chosen_start)
                        end_start = chosen_end - step
                        end_idx = starts.index(end_start)
                        ev_req = optimizer.EvRequest(start_idx, end_idx, required, cfg.ev.planning_power_kw)
                    except ValueError:
                        # Nemělo by nastat pro doporučení vytvořené z `starts`,
                        # ale forecast summary necháme zapsat a optimizer poběží
                        # bez EV hard požadavku místo pádu celého planneru.
                        ev_req = None
                active_summary.append({
                    "type": rtype,
                    "id": req.get("id") or req.get("request_id"),
                    "requested_ac_kwh_original": original_required,
                    "required_ac_kwh": effective_target,
                    "delivered_kwh": delivered,
                    "request_remaining_kwh": required,
                    "deadline": deadline.isoformat(),
                    "deadline_outside_current_horizon": deadline > horizon_end if horizon_end is not None else False,
                    "recommendation": {
                        "feasible": chosen_feasible,
                        "recommended_start": chosen_start.isoformat() if chosen_start else None,
                        "latest_safe_start": safe_start.isoformat(),
                        "expected_end": chosen_end.isoformat() if chosen_end else None,
                        "expected_delivered_kwh": expected_delivered,
                        "reason": chosen_reason,
                    },
                })

    return ev_req, boiler_req, active_summary


def boiler_daily_budget(starts: list[datetime], cfg: Config, now: datetime, ledger: Any) -> tuple[dict, dict]:
    """Return per-local-date MILP limits and planner diagnostics."""
    state = boiler_state.normalize_state(ledger)
    days = state.get("days", {})
    today = days.get(now.date().isoformat(), {})
    delivered_today = float(today.get("estimated_delivered_kwh", 0.0) or 0.0)
    commanded_today = float(today.get("commanded_kwh", 0.0) or 0.0)
    limits = {}
    diagnostics = {}
    for local_date in sorted({start.date() for start in starts}):
        delivered = delivered_today if local_date == now.date() else 0.0
        remaining = max(0.0, cfg.boiler.opportunistic_daily_limit_kwh - delivered)
        limits[local_date] = remaining
        diagnostics[local_date.isoformat()] = {
            "daily_limit_kwh": round(cfg.boiler.opportunistic_daily_limit_kwh, 6),
            "commanded_before_plan_kwh": round(commanded_today if local_date == now.date() else 0.0, 6),
            "estimated_delivered_before_plan_kwh": round(delivered, 6),
            "remaining_planner_budget_kwh": round(remaining, 6),
        }
    return limits, diagnostics


async def read_live_state() -> dict:
    """Read-only načtení runtime dat střídače přes ověřený GoodWe pattern."""
    if GOODWE_LIB_DIR not in sys.path:
        sys.path.insert(0, GOODWE_LIB_DIR)
    import goodwe  # noqa: PLC0415

    parser = configparser.ConfigParser()
    parser.read(GOODWE_CONF_PATH)
    ip_address = parser["settings"]["ip_address"]
    inverter = await goodwe.connect(ip_address)
    data = await inverter.read_runtime_data()
    out = {}
    for sensor in inverter.sensors():
        if sensor.id_ in data:
            out[sensor.id_] = data[sensor.id_]
    return {
        "inverter_reachable": True,
        "battery_soc": out.get("battery_soc"),
        "house_consumption": out.get("house_consumption"),
        "load_p1": out.get("load_p1"),
        "load_p2": out.get("load_p2"),
        "load_p3": out.get("load_p3"),
        "ppv1": out.get("ppv1", 0),
        "ppv2": out.get("ppv2", 0),
        "meter_active_power_total": out.get("meter_active_power_total"),
        "meter_active_power1": out.get("meter_active_power1"),
        "meter_active_power2": out.get("meter_active_power2"),
        "meter_active_power3": out.get("meter_active_power3"),
        "igrid1": out.get("igrid1"),
        "igrid2": out.get("igrid2"),
        "igrid3": out.get("igrid3"),
        "work_mode_label": out.get("work_mode_label"),
    }


def soc_pct_to_kwh(soc_pct: float, cfg: Config) -> float:
    return max(0.0, min(100.0, soc_pct)) / 100.0 * cfg.battery.capacity_kwh


def battery_power_kw(slot: optimizer.SlotResult, cfg: Config) -> float:
    slot_hours = cfg.system.planning_step_minutes / 60.0
    charge = slot.pv_to_battery_kwh + slot.grid_to_battery_kwh
    discharge = slot.battery_to_fixed_load_kwh + slot.battery_to_boiler_kwh + slot.battery_to_grid_kwh
    return (charge - discharge) / slot_hours


def reason_codes_for(meta: SlotPlanInput, slot: optimizer.SlotResult) -> list[str]:
    codes = [f"BATTERY_{slot.battery_action}"]
    if meta.price_source != "actual":
        codes.append("PRICE_ESTIMATED")
    if not meta.export_allowed:
        codes.append("EXPORT_DISABLED_BY_PRICE")
    if meta.effective_import_nonpositive:
        codes.append("VERY_CHEAP_IMPORT_NO_DISCHARGE")
    if meta.base_load_source != "profile":
        codes.append("BASE_LOAD_FALLBACK")
    if meta.pool_load_kwh > 0:
        codes.append("POOL_CIRCULATION_FIXED_LOAD")
    if slot.boiler_hard_kwh > 1e-6:
        codes.append("BOILER_HARD_REQUEST")
    if slot.boiler_opportunistic_kwh > 1e-6:
        codes.append("BOILER_OPPORTUNISTIC_ECONOMIC")
    return codes


def build_forecast_document(
    *,
    generated_at: datetime,
    valid_until: datetime,
    cfg: Config,
    live_state: dict,
    meta: list[SlotPlanInput],
    result: optimizer.OptimizerResult,
    active_requests: list[dict],
    terminal_value_czk_per_kwh: float,
    additional_requests: Optional[list[dict]] = None,
    detected_loads: Optional[dict] = None,
    planner_duration_seconds: Optional[float] = None,
    boiler_budget_diagnostics: Optional[dict] = None,
    ev_charging_session: Optional[dict] = None,
) -> dict:
    capacity = cfg.battery.capacity_kwh
    slots_json = []
    result_by_start = {s.slot_start: s for s in result.slots}
    additional_requests = additional_requests or []
    for m in meta:
        r = result_by_start.get(m.slot_start)
        if r is None:
            slots_json.append({
                "slot_start": m.slot_start.isoformat(),
                "price_eur_mwh": round(m.price_eur_mwh, 4),
                "price_source": m.price_source,
                "import_price_czk_kwh": round(m.import_price_czk_kwh, 6),
                "export_revenue_czk_kwh": round(m.export_revenue_czk_kwh, 6),
                "pv_estimate_kwh": round(m.pv_estimate_kwh, 6),
                "base_load_expected_kwh": round(m.base_load_expected_kwh, 6),
                "base_load_reserve_kwh": round(m.base_load_reserve_kwh, 6),
                "pool_load_kwh": round(m.pool_load_kwh, 6),
                "fixed_load_kwh": round(m.fixed_load_kwh, 6),
                "ev_load_kwh": 0.0,
                "boiler_power_kw": 0.0,
                "boiler_hard_kwh": 0.0,
                "boiler_opportunistic_kwh": 0.0,
                "additional_load_kwh": round(m.additional_load_kwh, 6),
                "additional_load_breakdown": additional_load_breakdown_for_slot(
                    m.slot_start, cfg.system.planning_step_minutes, additional_requests, detected_loads
                ),
                "reason_codes": ["OPTIMIZER_NO_SLOT_RESULT"],
                "evidence": {"price_export_eur_mwh": round(m.price_export_eur_mwh, 4)},
            })
            continue

        bp_kw = battery_power_kw(r, cfg)
        boiler_power_kw = sum(1 for on in r.boiler_phase_on if on) * cfg.boiler.phase_power_kw
        slots_json.append({
            "slot_start": m.slot_start.isoformat(),
            "price_eur_mwh": round(m.price_eur_mwh, 4),
            "price_source": m.price_source,
            "import_price_czk_kwh": round(m.import_price_czk_kwh, 6),
            "export_revenue_czk_kwh": round(m.export_revenue_czk_kwh, 6),
            "pv_estimate_kwh": round(m.pv_estimate_kwh, 6),
            "base_load_expected_kwh": round(m.base_load_expected_kwh, 6),
            "base_load_reserve_kwh": round(m.base_load_reserve_kwh, 6),
            "pool_load_kwh": round(m.pool_load_kwh + m.pool_heat_pump_kwh, 6),
            "fixed_load_kwh": round(m.fixed_load_kwh, 6),
            "ev_load_kwh": round(r.ev_delivered_kwh, 6),
            "additional_load_kwh": round(m.additional_load_kwh, 6),
            "additional_load_breakdown": additional_load_breakdown_for_slot(
                m.slot_start, cfg.system.planning_step_minutes, additional_requests, detected_loads
            ),
            "soc_start_pct": round(r.soc_start_kwh / capacity * 100.0, 3),
            "soc_end_pct": round(r.soc_end_kwh / capacity * 100.0, 3),
            "battery_action": r.battery_action,
            "battery_power_kw": round(bp_kw, 6),
            "grid_import_kwh": round(r.grid_import_kwh, 6),
            "grid_export_kwh": round(r.grid_export_kwh, 6),
            "boiler_power_kw": round(boiler_power_kw, 6),
            "boiler_hard_kwh": round(r.boiler_hard_kwh, 6),
            "boiler_opportunistic_kwh": round(r.boiler_opportunistic_kwh, 6),
            "reason_codes": reason_codes_for(m, r),
            "evidence": {
                "price_export_eur_mwh": round(m.price_export_eur_mwh, 4),
                "export_allowed": m.export_allowed,
                "effective_import_nonpositive": m.effective_import_nonpositive,
                "base_load_source": m.base_load_source,
                "sun_pct": m.sun_pct,
                "cloudcover_pct": m.cloudcover_pct,
                "pv_to_battery_kwh": round(r.pv_to_battery_kwh, 6),
                "grid_to_battery_kwh": round(r.grid_to_battery_kwh, 6),
                "battery_to_grid_kwh": round(r.battery_to_grid_kwh, 6),
                "battery_to_fixed_load_kwh": round(r.battery_to_fixed_load_kwh, 6),
                "battery_to_boiler_kwh": round(r.battery_to_boiler_kwh, 6),
                "boiler_phase_on": list(r.boiler_phase_on),
            },
        })

    slot_hours = cfg.system.planning_step_minutes / 60.0
    report_tz = ZoneInfo(cfg.system.timezone)
    generated_at_for_summary = generated_at.replace(tzinfo=report_tz) if generated_at.tzinfo is None else generated_at
    first_24_end = generated_at_for_summary + timedelta(hours=24)
    first_24_slots = [
        s for s in slots_json
        if _same_tz(
            parse_iso_datetime(s["slot_start"], report_tz),
            first_24_end,
            report_tz,
        ) < first_24_end
    ]
    per_day_boiler = {}
    for slot in slots_json:
        local_date = parse_iso_datetime(slot["slot_start"], report_tz).date().isoformat()
        day = per_day_boiler.setdefault(local_date, {"planned_hard_kwh": 0.0, "planned_opportunistic_kwh": 0.0})
        day["planned_hard_kwh"] += float(slot.get("boiler_hard_kwh", 0.0) or 0.0)
        day["planned_opportunistic_kwh"] += float(slot.get("boiler_opportunistic_kwh", 0.0) or 0.0)
    for local_date, values in per_day_boiler.items():
        values.update((boiler_budget_diagnostics or {}).get(local_date, {}))
        values["planned_hard_kwh"] = round(values["planned_hard_kwh"], 6)
        values["planned_opportunistic_kwh"] = round(values["planned_opportunistic_kwh"], 6)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "valid_until": valid_until.isoformat(),
        "config_hash": cfg.config_hash,
        "model_version": MODEL_VERSION,
        "dry_run": cfg.system.dry_run,
        "write_gates": {
            "battery_write_enabled": cfg.system.battery_write_enabled,
            "boiler_write_enabled": cfg.system.boiler_write_enabled,
            "whatsapp_listener_enabled": cfg.system.whatsapp_listener_enabled,
        },
        "solver": {"name": "PuLP+CBC", "status": result.status},
        "economics": {
            "eur_czk": cfg.economics.czk_per_eur,
            "battery_cycle_eur_mwh": cfg.economics.battery_cycle_cost_eur_per_mwh,
            "gas_heat_eur_mwh": cfg.economics.gas_heat_value_eur_per_mwh,
            "terminal_value_czk_kwh": round(terminal_value_czk_per_kwh, 6),
            "economic_objective_czk": round(result.economic_objective_czk, 6),
        },
        "diagnostics": {
            "planner_version": VERSION,
            "planner_duration_seconds": None if planner_duration_seconds is None else round(planner_duration_seconds, 3),
            "planned_boiler_kwh_next_24h": round(sum(float(s.get("boiler_power_kw", 0.0) or 0.0) * slot_hours for s in first_24_slots), 6),
            "planned_boiler_hard_kwh_next_24h": round(sum(float(s.get("boiler_hard_kwh", 0.0) or 0.0) for s in first_24_slots), 6),
            "planned_boiler_opportunistic_kwh_next_24h": round(sum(float(s.get("boiler_opportunistic_kwh", 0.0) or 0.0) for s in first_24_slots), 6),
            "boiler_opportunistic_daily_limit_kwh": round(cfg.boiler.opportunistic_daily_limit_kwh, 6),
            "boiler_daily_budget": per_day_boiler,
            "planned_ev_kwh_next_24h": round(sum(float(s.get("ev_load_kwh", 0.0) or 0.0) for s in first_24_slots), 6),
            "planned_fixed_load_kwh_next_24h": round(sum(float(s.get("fixed_load_kwh", 0.0) or 0.0) for s in first_24_slots), 6),
            "planned_additional_load_kwh_next_24h": round(sum(float(s.get("additional_load_kwh", 0.0) or 0.0) for s in first_24_slots), 6),
        },
        "live_state": live_state,
        "current_soc_pct": live_state.get("battery_soc"),
        "ev_charging_session": ev_charging_session or {},
        "active_requests": active_requests,
        "optimizer_slacks": {
            "ev_unserved_kwh": round(result.ev_unserved_kwh, 6),
            "boiler_hard_unserved_kwh": round(result.boiler_hard_unserved_kwh, 6),
        },
        "slots": slots_json,
    }


def _request_alert_key(req: dict) -> str:
    return str(req.get("id") or req.get("request_id") or req.get("type") or "unknown")


def _is_deferred_far_horizon_request(req: dict) -> bool:
    rec = req.get("recommendation", {}) if isinstance(req.get("recommendation"), dict) else {}
    reason = str(rec.get("reason") or "")
    return bool(req.get("deadline_outside_current_horizon")) and "mimo aktuální 48h horizont" in reason


def _format_ev_alert_interval(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start.day}. {start.month}. {start.year} {start:%H:%M}–{end:%H:%M}"
    return f"{start.day}. {start.month}. {start.year} {start:%H:%M} až {end.day}. {end.month}. {end.year} {end:%H:%M}"


def send_ev_schedule_change_alerts(
    *,
    now: datetime,
    cfg: Config,
    active_requests: list[dict],
    requests_path: Path = REQUESTS_PATH,
    alert_state_path: Path = ALERT_STATE_PATH,
) -> list[dict]:
    """Notify when an active EV start moves by at least the business threshold."""

    outcomes: list[dict] = []
    for req in active_requests:
        if not isinstance(req, dict) or req.get("type") != "ev_charge":
            continue
        if req.get("window_locked") or req.get("session_status") in ev_session.ACTIVE_STATES:
            continue
        request_id = _request_alert_key(req)
        rec = req.get("recommendation", {}) if isinstance(req.get("recommendation"), dict) else {}
        if (
            rec.get("feasible") is not True
            or not rec.get("recommended_start")
            or not rec.get("expected_end")
        ):
            continue
        try:
            new_start = parse_iso_datetime(str(rec["recommended_start"]), ZoneInfo(cfg.system.timezone))
            new_end = parse_iso_datetime(str(rec["expected_end"]), ZoneInfo(cfg.system.timezone))
        except (TypeError, ValueError):
            continue
        with request_store.active_ev_schedule_notification(requests_path, request_id) as notification:
            if notification is None:
                continue
            previous_start = parse_iso_datetime(notification.last_notified_start, ZoneInfo(cfg.system.timezone))
            shift_minutes = abs((new_start - previous_start).total_seconds()) / 60.0
            if shift_minutes < EV_SCHEDULE_CHANGE_MINUTES:
                continue
            direction = "později" if new_start > previous_start else "dříve"
            message = (
                "FVE INFO: doporučený plán nabíjení auta se významně změnil. "
                f"Nové okno: {_format_ev_alert_interval(new_start, new_end)}. "
                f"Předchozí oznámený start byl {previous_start:%H:%M}; "
                f"nový start je o {shift_minutes:.0f} minut {direction}."
            )
            outcome = alerting.notify_once(
                f"planner.ev_schedule_changed.{request_id}", message,
                cfg=cfg, state_path=alert_state_path, now=now,
            )
            if outcome.get("sent") is True or outcome.get("reason") == "deduplicated":
                update = request_store.compare_and_set_ev_schedule_notification(
                    requests_path, request_id,
                    expected_last_start=notification.last_notified_start,
                    new_start=str(rec["recommended_start"]), new_end=str(rec["expected_end"]),
                    notified_at=now.isoformat(timespec="seconds"), lock_held=True,
                )
                outcome["baseline_updated"] = update.updated
                outcome["baseline_update_reason"] = update.reason
                outcome["shift_minutes"] = shift_minutes
            outcomes.append(outcome)
    return outcomes


def send_planner_alerts(
    *,
    now: datetime,
    cfg: Config,
    result: optimizer.OptimizerResult,
    active_requests: list[dict],
    requests_path: Path = REQUESTS_PATH,
    alert_state_path: Path = ALERT_STATE_PATH,
) -> list[dict]:
    """Send deduplicated planner-side alerts via notify_admins.sh."""

    outcomes: list[dict] = []
    if result.status != "optimal":
        outcomes.append(alerting.notify_once(
            "planner.solver_non_optimal",
            f"FVE ALERT: planner skončil se solver statusem {result.status}; nový plán nemusí být použitelný.",
            cfg=cfg,
            state_path=alert_state_path,
            now=now,
        ))

    if result.ev_unserved_kwh > 1e-6:
        outcomes.append(alerting.notify_once(
            "planner.ev_unserved_slack",
            f"FVE ALERT: EV požadavek není plně splnitelný v MILP plánu, nedodáno {result.ev_unserved_kwh:.2f} kWh.",
            cfg=cfg,
            state_path=alert_state_path,
            now=now,
        ))

    if result.boiler_hard_unserved_kwh > 1e-6:
        outcomes.append(alerting.notify_once(
            "planner.boiler_unserved_slack",
            f"FVE ALERT: požadavek na nahřátí bojleru není plně splnitelný, nedodáno {result.boiler_hard_unserved_kwh:.2f} kWh.",
            cfg=cfg,
            state_path=alert_state_path,
            now=now,
        ))

    for req in active_requests:
        if not isinstance(req, dict) or req.get("type") != "ev_charge":
            continue
        rec = req.get("recommendation", {}) if isinstance(req.get("recommendation"), dict) else {}
        if rec.get("feasible") is False and not _is_deferred_far_horizon_request(req):
            outcomes.append(alerting.notify_once(
                f"planner.ev_request_infeasible.{_request_alert_key(req)}",
                "FVE ALERT: požadavek na nabití auta není podle aktuální analýzy splnitelný. "
                f"Požadavek: {req.get('required_ac_kwh')} kWh do {req.get('deadline')}. "
                f"Důvod: {rec.get('reason')}",
                cfg=cfg,
                state_path=alert_state_path,
                now=now,
            ))

    outcomes.extend(send_ev_schedule_change_alerts(
        now=now,
        cfg=cfg,
        active_requests=active_requests,
        requests_path=requests_path,
        alert_state_path=alert_state_path,
    ))

    return outcomes


def run_planner(
    *,
    cfg: Config,
    now: datetime,
    live_state: dict,
    output_path: Path = DEFAULT_FORECAST_PATH,
    verbose: bool = True,
) -> dict:
    started_monotonic = time.monotonic()
    starts = slot_starts(now, cfg)
    tz = ZoneInfo(cfg.system.timezone)
    expired_request_ids = request_store.expire_past_requests(REQUESTS_PATH, now=now)
    if expired_request_ids:
        log(f"Označeno jako prošlé requesty: {', '.join(expired_request_ids)}", verbose=verbose)
    additional_requests = load_additional_load_requests(REQUESTS_PATH, tz)
    detected_loads = read_json(DETECTED_LOADS_PATH, {})
    ev_session_state = ev_session.read_state(EV_SESSION_STATE_PATH)
    planning_detected_loads = detected_loads_without_session_ev(detected_loads, ev_session_state)
    opt_slots, meta = build_plan_inputs(
        starts,
        cfg,
        additional_requests=additional_requests,
        detected_loads=planning_detected_loads,
    )
    terminal_value = compute_terminal_value_czk_per_kwh(meta, cfg)
    initial_soc = soc_pct_to_kwh(float(live_state["battery_soc"]), cfg)

    requests_list = load_active_requests(REQUESTS_PATH, tz)
    boiler_ledger = read_json(BOILER_CONTROL_STATE_PATH, {})
    boiler_daily_limits, boiler_budget_diagnostics = boiler_daily_budget(starts, cfg, now, boiler_ledger)
    ev_req, boiler_req, active_summary = choose_requests(
        requests_list, starts, cfg, opt_slots, initial_soc, terminal_value, boiler_daily_limits,
        ev_session_state=ev_session_state,
    )

    log(f"Optimalizuji {len(opt_slots)} slotů, SoC={live_state['battery_soc']} %, terminal={terminal_value:.4f} CZK/kWh", verbose=verbose)
    result = optimizer.optimize(
        opt_slots,
        cfg,
        initial_soc_kwh=initial_soc,
        terminal_value_czk_per_kwh=terminal_value,
        ev_request=ev_req,
        boiler_hard_request=boiler_req,
        boiler_opportunistic_daily_limits_kwh=boiler_daily_limits,
    )
    planner_duration_seconds = time.monotonic() - started_monotonic
    valid_until = now + timedelta(minutes=cfg.system.plan_max_age_minutes)
    doc = build_forecast_document(
        generated_at=now,
        valid_until=valid_until,
        cfg=cfg,
        live_state=live_state,
        meta=meta,
        result=result,
        active_requests=active_summary,
        terminal_value_czk_per_kwh=terminal_value,
        additional_requests=additional_requests,
        detected_loads=planning_detected_loads,
        planner_duration_seconds=planner_duration_seconds,
        boiler_budget_diagnostics=boiler_budget_diagnostics,
        ev_charging_session=ev_session_state,
    )
    alert_outcomes = send_planner_alerts(now=now, cfg=cfg, result=result, active_requests=active_summary)
    doc["alerts"] = alert_outcomes
    atomic_write_json(output_path, doc)
    log(f"Zapsáno {output_path} ({len(doc['slots'])} slotů, status={result.status}, duration={planner_duration_seconds:.1f}s)", verbose=verbose)
    return doc


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="planner.py v11 MILP orchestrace")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Cesta ke config.toml")
    parser.add_argument("--output", default=str(DEFAULT_FORECAST_PATH), help="Výstupní forecast_48h.json")
    parser.add_argument("--initial-soc", type=float, default=None, help="Testovací override SoC; přeskočí live GoodWe read")
    parser.add_argument("--dry-run", action="store_true", help="Kompatibilní flag; planner nikdy nezapisuje do zařízení")
    parser.add_argument("--verbose", action="store_true", help="Verbose log na stdout")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    tz = ZoneInfo(cfg.system.timezone)
    now = datetime.now(tz)
    log(f"=== planner.py v11 start (version={VERSION}, config dry_run={cfg.system.dry_run}) ===", verbose=args.verbose)

    if args.initial_soc is not None:
        live_state = {"inverter_reachable": False, "battery_soc": args.initial_soc, "source": "--initial-soc"}
    else:
        try:
            live_state = asyncio.run(read_live_state())
        except Exception as e:  # read-only fail-safe: bez live SoC neplánovat spekulativně
            print(f"CHYBA: nelze přečíst live stav střídače: {e}", file=sys.stderr)
            alerting.notify_once(
                "planner.inverter_read_failed",
                f"FVE ALERT: planner nedokázal přečíst live stav střídače: {e}",
                cfg=cfg,
                state_path=ALERT_STATE_PATH,
                now=now,
            )
            return 3

    if live_state.get("battery_soc") is None:
        print("CHYBA: live_state neobsahuje battery_soc", file=sys.stderr)
        alerting.notify_once(
            "planner.inverter_soc_missing",
            "FVE ALERT: planner přečetl live stav střídače, ale chybí battery_soc; plán nebyl obnoven.",
            cfg=cfg,
            state_path=ALERT_STATE_PATH,
            now=now,
        )
        return 3

    run_planner(
        cfg=cfg,
        now=now,
        live_state=live_state,
        output_path=Path(args.output),
        verbose=args.verbose,
    )
    log("=== planner.py v11 konec ===", verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())