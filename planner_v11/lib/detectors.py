"""
Live load detectors for planner_v10 executor.

VERSION = "1.3"

Changelog:
- v1.3 (2026-07-25): Add a conservative sanity ceiling
  (`UNEXPECTED_SANITY_MAX_KW`) so an implausibly large single-phase reading
  (data glitch, not a real appliance) surfaces only as a diagnostic
  `SUSPICIOUS_HIGH_UNEXPECTED_LOAD_READING` data_quality flag instead of
  immediately confirming/extending `unexpected_load.active`.
- v1.2 (2026-07-25): Treat live GoodWe power fields consistently as watts,
  fix EV fallback confirmation to count candidate phase-load samples, shorten
  unexpected-load active state to current confirmed anomalies, and expose a
  conservative unannounced EV planning assumption (8 kWh / 5 h).
- v1.1 (2026-07-24): Prefer direct read-only wallbox API power for immediate
  EV charging detection, with phase-load heuristic fallback.
- v1.0 (2026-07-22): Add conservative read-only EV/pool/boiler/unexpected-load
  accounting used by executor runtime_state and short-term planning adjustment.

Authoritative sources:
  - ARCHITECTURE_DESIGN_v10_FINAL.md section 14: unexplained_load formula,
    tracking of size/phase/duration and temporary projection of active larger
    anomalies into the plan.
  - CONTROL_LOGIC_SPEC_v10.yaml sections `vehicle.detector`, `pool`, `boiler`,
    `unexpected_load`.

This module is deliberately pure and side-effect free. It does not write to any
device and does not send notifications. The executor owns persistence.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from . import pool_model


VERSION = "1.3"

EV_START_RESIDUAL_KW = 2.4
EV_START_REQUIRED_SAMPLES_OF_5 = 3
EV_FULL_RANGE_KW = (2.7, 3.4)
EV_TAPER_RANGE_KW = (0.4, 2.7)
EV_STOP_BELOW_KW = 0.4

POOL_SIGNATURE_TOLERANCE_KW = 0.15
BOILER_STEP_TOLERANCE_KW = 0.30

UNEXPECTED_START_KW = 2.0
UNEXPECTED_REQUIRED_SAMPLES = 2
PLANNING_ADJUSTMENT_MIN_KW = 0.2
UNANNOUNCED_EV_ASSUMED_KWH = 8.0
UNANNOUNCED_EV_ASSUMED_HOURS = 5.0
# A single phase on a 32A/230V main breaker physically tops out around 7.4 kW.
# Anything reported above this sanity ceiling is far more likely a bad/glitched
# sensor reading than a real appliance, so it must NOT immediately confirm
# `unexpected_load.active`/trigger a replan without further validation - it is
# only surfaced as a diagnostic data-quality flag (HANDOFF finding #1, point 3).
UNEXPECTED_SANITY_MAX_KW = 10.0

PHASES = ("L1", "L2", "L3")
PHASE_LOAD_KEYS = {"L1": "load_p1", "L2": "load_p2", "L3": "load_p3"}


def _to_kw(value: Any) -> Optional[float]:
    """Convert a GoodWe live power value in W to kW.

    Runtime sensors used by executor (`house_consumption`, `load_p1/2/3`,
    `ppv1/2`, `meter_active_power*`) are raw watts even for small values like
    25 W. Never guess kW from magnitude here; tests must use the same W
    convention as live data.
    """
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric / 1000.0


def phase_load_kw(live_state: dict, phase: str) -> Optional[float]:
    return _to_kw(live_state.get(PHASE_LOAD_KEYS[phase]))


def measured_house_kw(live_state: dict) -> Optional[float]:
    direct = _to_kw(live_state.get("house_consumption"))
    if direct is not None:
        return max(0.0, direct)
    phase_values = [phase_load_kw(live_state, p) for p in PHASES]
    if any(v is None for v in phase_values):
        return None
    return max(0.0, sum(float(v) for v in phase_values))


def slot_power_kw(slot: Optional[dict], key: str, slot_minutes: int) -> float:
    if not slot:
        return 0.0
    try:
        value = float(slot.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if key.endswith("_kwh"):
        return value / (slot_minutes / 60.0)
    return value


def _history_detector_items(recent_history: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in recent_history:
        detector = row.get("detected_loads") if isinstance(row, dict) else None
        if isinstance(detector, dict):
            out.append(detector)
    return out


def recent_ev_positive_samples(recent_history: list[dict]) -> int:
    count = 0
    for detector in _history_detector_items(recent_history)[-4:]:
        try:
            ev = detector.get("ev", {}) if isinstance(detector.get("ev"), dict) else {}
            detected_kw = float(ev.get("detected_kw", 0.0) or 0.0)
            phase_kw = float(ev.get("phase_load_kw", 0.0) or 0.0)
            state = str(ev.get("state", ""))
            if (
                detected_kw >= EV_START_RESIDUAL_KW
                or phase_kw >= EV_START_RESIDUAL_KW
                or state in ("CANDIDATE_WAITING_FOR_CONFIRMATION", "FULL", "TAPER_OR_PARTIAL", "CHARGING")
            ):
                count += 1
        except (TypeError, ValueError):
            pass
    return count


def recent_unexpected_positive_samples(recent_history: list[dict]) -> int:
    count = 0
    for detector in _history_detector_items(recent_history)[-4:]:
        try:
            if float(detector.get("unexpected_load", {}).get("kw", 0.0) or 0.0) >= UNEXPECTED_START_KW:
                count += 1
        except (TypeError, ValueError):
            pass
    return count


def detect_boiler_kw(live_state: dict, cfg: Any) -> tuple[float, dict[str, bool]]:
    per_phase: dict[str, bool] = {}
    total = 0.0
    phase_power = float(cfg.boiler.phase_power_kw)
    for idx, phase in enumerate(PHASES, start=1):
        kw = phase_load_kw(live_state, phase)
        detected = False
        if kw is not None and phase_power > 0:
            detected = abs(kw - phase_power) <= BOILER_STEP_TOLERANCE_KW
        per_phase[f"phase{idx}"] = detected
        if detected:
            total += phase_power
    return total, per_phase


def detect_ev_kw(
    live_state: dict,
    cfg: Any,
    recent_history: list[dict],
    wallbox_state: Optional[dict] = None,
) -> tuple[float, dict]:
    if isinstance(wallbox_state, dict) and wallbox_state.get("available"):
        try:
            wallbox_kw = float(wallbox_state.get("charging_power_kw") or 0.0)
        except (TypeError, ValueError):
            wallbox_kw = 0.0
        if wallbox_kw >= EV_STOP_BELOW_KW:
            detected_kw = min(wallbox_kw, float(cfg.ev.nominal_power_kw))
            in_full_range = EV_FULL_RANGE_KW[0] <= wallbox_kw <= EV_FULL_RANGE_KW[1]
            in_taper_range = EV_TAPER_RANGE_KW[0] <= wallbox_kw < EV_TAPER_RANGE_KW[1]
            state = "FULL" if in_full_range else "TAPER_OR_PARTIAL" if in_taper_range else "CHARGING"
            return detected_kw, {
                "available": True,
                "source": "wallbox_api",
                "phase": cfg.ev.phase,
                "state": state,
                "confidence": 1.0,
                "wallbox": wallbox_state,
                "full_range_kw": list(EV_FULL_RANGE_KW),
                "taper_range_kw": list(EV_TAPER_RANGE_KW),
            }
        return 0.0, {
            "available": True,
            "source": "wallbox_api",
            "phase": cfg.ev.phase,
            "state": "STOPPED",
            "confidence": 1.0,
            "wallbox": wallbox_state,
            "full_range_kw": list(EV_FULL_RANGE_KW),
            "taper_range_kw": list(EV_TAPER_RANGE_KW),
        }

    phase_kw = phase_load_kw(live_state, cfg.ev.phase)
    if phase_kw is None:
        return 0.0, {
            "available": False,
            "source": "phase_load_heuristic",
            "reason": "EV_PHASE_LOAD_UNAVAILABLE",
            "wallbox": wallbox_state,
        }

    current_positive = phase_kw >= EV_START_RESIDUAL_KW
    positive_samples = recent_ev_positive_samples(recent_history) + (1 if current_positive else 0)
    start_confirmed = positive_samples >= EV_START_REQUIRED_SAMPLES_OF_5
    in_full_range = EV_FULL_RANGE_KW[0] <= phase_kw <= EV_FULL_RANGE_KW[1]
    in_taper_range = EV_TAPER_RANGE_KW[0] <= phase_kw < EV_TAPER_RANGE_KW[1]

    if start_confirmed and (in_full_range or in_taper_range or current_positive):
        detected_kw = min(phase_kw, float(cfg.ev.nominal_power_kw))
        state = "FULL" if in_full_range else "TAPER_OR_PARTIAL"
    elif phase_kw < EV_STOP_BELOW_KW:
        detected_kw = 0.0
        state = "STOPPED"
    else:
        detected_kw = 0.0
        state = "CANDIDATE_WAITING_FOR_CONFIRMATION" if current_positive else "IDLE"

    confidence = min(1.0, positive_samples / EV_START_REQUIRED_SAMPLES_OF_5)
    return detected_kw, {
        "available": True,
        "source": "phase_load_heuristic",
        "phase": cfg.ev.phase,
        "phase_load_kw": round(phase_kw, 3),
        "positive_samples": positive_samples,
        "confidence": round(confidence, 3),
        "state": state,
        "wallbox": wallbox_state,
        "full_range_kw": list(EV_FULL_RANGE_KW),
        "taper_range_kw": list(EV_TAPER_RANGE_KW),
    }


def detect_pool_kw(now: datetime, live_state: dict, current_slot: Optional[dict], cfg: Any) -> tuple[float, dict]:
    flow_phase_kw = phase_load_kw(live_state, cfg.pool.flow_phase)
    planned_flow_kw = slot_power_kw(current_slot, "pool_load_kwh", cfg.system.planning_step_minutes)
    in_window = pool_model.is_in_any_window(now.replace(tzinfo=None), cfg)
    signature_seen = False
    if flow_phase_kw is not None:
        signature_seen = flow_phase_kw >= max(0.0, float(cfg.pool.flow_power_kw) - POOL_SIGNATURE_TOLERANCE_KW)

    circulation_kw = float(cfg.pool.flow_power_kw) if (signature_seen or planned_flow_kw > 0.0) else 0.0

    heat_phase_kw = phase_load_kw(live_state, cfg.pool.heat_pump_phase)
    heat_kw = 0.0
    if in_window and heat_phase_kw is not None:
        residual = heat_phase_kw
        if cfg.pool.heat_pump_phase == cfg.pool.flow_phase:
            residual -= circulation_kw
        heat_kw = max(0.0, min(float(cfg.pool.heat_pump_max_kw), residual))
        if heat_kw < 0.2:
            heat_kw = 0.0

    reason = "POOL_EXPECTED_LOAD" if planned_flow_kw > 0 else "POOL_SIGNATURE_OUTSIDE_WINDOW" if signature_seen else "POOL_IDLE"
    if planned_flow_kw > 0 and not signature_seen:
        reason = "POOL_EXPECTED_LOAD_UNCONFIRMED"

    return circulation_kw + heat_kw, {
        "flow_phase": cfg.pool.flow_phase,
        "heat_pump_phase": cfg.pool.heat_pump_phase,
        "in_window": in_window,
        "planned_flow_kw": round(planned_flow_kw, 3),
        "flow_phase_load_kw": None if flow_phase_kw is None else round(flow_phase_kw, 3),
        "signature_seen": signature_seen,
        "circulation_kw": round(circulation_kw, 3),
        "heat_pump_kw": round(heat_kw, 3),
        "reason": reason,
    }


def _phase_residuals(
    live_state: dict,
    cfg: Any,
    *,
    ev_kw: float,
    pool_kw: float,
    boiler_phases: dict[str, bool],
) -> dict[str, Optional[float]]:
    residuals: dict[str, Optional[float]] = {}
    for idx, phase in enumerate(PHASES, start=1):
        kw = phase_load_kw(live_state, phase)
        if kw is None:
            residuals[phase] = None
            continue
        residual = kw
        if phase == cfg.ev.phase:
            residual -= ev_kw
        if phase == cfg.pool.flow_phase:
            residual -= min(pool_kw, float(cfg.pool.flow_power_kw))
        if boiler_phases.get(f"phase{idx}"):
            residual -= float(cfg.boiler.phase_power_kw)
        residuals[phase] = round(max(0.0, residual), 3)
    return residuals


def dominant_phase(residuals: dict[str, Optional[float]]) -> Optional[str]:
    available = {p: v for p, v in residuals.items() if v is not None}
    if not available:
        return None
    return max(available, key=lambda p: float(available[p]))


def detect_loads(
    *,
    now: datetime,
    cfg: Any,
    live_state: dict,
    current_slot: Optional[dict],
    recent_history: Optional[list[dict]] = None,
    previous_state: Optional[dict] = None,
    wallbox_state: Optional[dict] = None,
) -> dict:
    """Return a serializable detector state for one executor run."""
    recent_history = recent_history or []
    previous_state = previous_state or {}

    house_kw = measured_house_kw(live_state)
    ev_kw, ev_evidence = detect_ev_kw(live_state, cfg, recent_history, wallbox_state)
    pool_kw, pool_evidence = detect_pool_kw(now, live_state, current_slot, cfg)
    boiler_kw, boiler_phases = detect_boiler_kw(live_state, cfg)
    announced_kw = slot_power_kw(current_slot, "additional_load_kwh", cfg.system.planning_step_minutes)

    if house_kw is None:
        unexplained_kw = 0.0
        data_quality = "MEASURED_HOUSE_LOAD_UNAVAILABLE"
    else:
        unexplained_kw = max(0.0, house_kw - ev_kw - pool_kw - boiler_kw - announced_kw)
        data_quality = "OK"

    planned_ev_kw = slot_power_kw(current_slot, "ev_load_kwh", cfg.system.planning_step_minutes)
    planned_pool_kw = slot_power_kw(current_slot, "pool_load_kwh", cfg.system.planning_step_minutes)
    planned_boiler_kw = slot_power_kw(current_slot, "boiler_power_kw", cfg.system.planning_step_minutes)

    current_unexpected_positive = unexplained_kw >= UNEXPECTED_START_KW
    suspicious_reading = unexplained_kw > UNEXPECTED_SANITY_MAX_KW
    if suspicious_reading:
        # Do not let an implausible single-sample spike confirm/extend the
        # active anomaly state; keep it visible only as diagnostics.
        current_unexpected_positive = False
        if data_quality == "OK":
            data_quality = "SUSPICIOUS_HIGH_UNEXPECTED_LOAD_READING"
    positive_unexpected = recent_unexpected_positive_samples(recent_history) + (
        1 if current_unexpected_positive else 0
    )
    unexpected_active = current_unexpected_positive and positive_unexpected >= UNEXPECTED_REQUIRED_SAMPLES
    previous_unexpected = previous_state.get("unexpected_load", {}) if isinstance(previous_state, dict) else {}
    previous_started_at = previous_unexpected.get("started_at") if isinstance(previous_unexpected, dict) else None
    started_at = previous_started_at if unexpected_active and previous_started_at else now.isoformat() if unexpected_active else None

    residuals = _phase_residuals(live_state, cfg, ev_kw=ev_kw, pool_kw=pool_kw, boiler_phases=boiler_phases)

    unannounced_ev_active = ev_kw > 0.0 and planned_ev_kw <= 0.05
    unannounced_ev_power_kw = (
        UNANNOUNCED_EV_ASSUMED_KWH / UNANNOUNCED_EV_ASSUMED_HOURS
        if unannounced_ev_active else 0.0
    )

    # Planned EV requests are already part of `ev_load_kwh`. If EV charging is
    # detected without an explicit plan, do not project just the instantaneous
    # current power for one hour. Instead expose a separate conservative
    # 8 kWh / 5 h unannounced EV assumption for the planner.
    planning_adjustment_kw = 0.0 if unannounced_ev_active else max(0.0, ev_kw - planned_ev_kw)
    planning_adjustment_kw += max(0.0, pool_kw - planned_pool_kw)
    planning_adjustment_kw += max(0.0, boiler_kw - planned_boiler_kw)
    # ARCH section 14 says only an active larger anomaly is projected
    # temporarily into the plan. Short spikes/kettle-like residuals remain only
    # diagnostic until the calibrated size+duration threshold is crossed.
    if unexpected_active:
        planning_adjustment_kw += unexplained_kw
    if planning_adjustment_kw < PLANNING_ADJUSTMENT_MIN_KW:
        planning_adjustment_kw = 0.0

    reason_codes: list[str] = []
    if ev_kw > 0:
        reason_codes.append("EV_DETECTED")
    if pool_kw > 0:
        reason_codes.append(pool_evidence["reason"])
    if boiler_kw > 0:
        reason_codes.append("BOILER_HEATING_DETECTED")
    if unexpected_active:
        reason_codes.append("UNEXPECTED_LOAD_REPLAN")
    if suspicious_reading:
        reason_codes.append("UNEXPECTED_LOAD_SUSPICIOUS_SANITY_CAP")

    return {
        "schema_version": 10,
        "detector_version": VERSION,
        "ts": now.isoformat(),
        "data_quality": data_quality,
        "measured_house_kw": None if house_kw is None else round(house_kw, 3),
        "ev": {"detected_kw": round(ev_kw, 3), **ev_evidence},
        "pool": {"detected_kw": round(pool_kw, 3), **pool_evidence},
        "boiler": {
            "detected_kw": round(boiler_kw, 3),
            "phase_power_kw": cfg.boiler.phase_power_kw,
            "phases": boiler_phases,
        },
        "announced_additional_load_kw": round(announced_kw, 3),
        "unexpected_load": {
            "kw": round(unexplained_kw, 3),
            "active": unexpected_active,
            "positive_samples": positive_unexpected,
            "threshold_kw": UNEXPECTED_START_KW,
            "started_at": started_at,
            "dominant_phase": dominant_phase(residuals),
            "phase_residual_kw": residuals,
            "replan_recommended": unexpected_active,
        },
        "unannounced_ev_load": {
            "active": unannounced_ev_active,
            "power_kw": round(unannounced_ev_power_kw, 3),
            "assumed_total_kwh": UNANNOUNCED_EV_ASSUMED_KWH if unannounced_ev_active else 0.0,
            "assumed_duration_hours": UNANNOUNCED_EV_ASSUMED_HOURS if unannounced_ev_active else 0.0,
            "valid_until": (now + timedelta(hours=UNANNOUNCED_EV_ASSUMED_HOURS)).isoformat() if unannounced_ev_active else None,
            "reason": "UNANNOUNCED_EV_CHARGING_DETECTED" if unannounced_ev_active else "NO_UNANNOUNCED_EV",
        },
        "planning_adjustment": {
            "kw": round(planning_adjustment_kw, 3),
            "valid_until": (now + timedelta(minutes=60)).isoformat() if planning_adjustment_kw > 0 else None,
            "reason": "ACTIVE_DETECTED_LOADS" if planning_adjustment_kw > 0 else "NO_ADJUSTMENT",
        },
        "reason_codes": reason_codes,
    }