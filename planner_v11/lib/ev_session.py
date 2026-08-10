"""Persistent physical EV charging-session state machine.

VERSION = "1.1"

Changelog:
- v1.1 (2026-08-10): Add a session-identity guarded persistence helper for
  completion notification delivery metadata.
- v1.0 (2026-08-07): Track wallbox-backed ACTIVE/PAUSED/CLOSED sessions,
  bind one user or synthetic target, and provide atomic replan claims.

The wallbox session counter is authoritative and already expressed in kWh for
the current physical charge. No legacy energy correction is applied here.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import request_store


VERSION = "1.1"
SCHEMA_VERSION = 1

MAX_SESSION_KWH = 9.0
SYNTHETIC_TARGET_KWH = 6.0
ACTIVE_POWER_THRESHOLD_W = 10.0
SESSION_END_GAP_MINUTES = 30.0
REPLAN_DEVIATION_KWH = 2.0

ACTIVE_STATES = ("ACTIVE", "PAUSED")


def _iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def _parse_datetime(value: Any, now: datetime) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=now.tzinfo)
    if now.tzinfo is not None:
        return parsed.astimezone(now.tzinfo)
    return parsed


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def request_identity(request: dict[str, Any] | None) -> str | None:
    if not isinstance(request, dict):
        return None
    value = request.get("id") or request.get("request_id")
    return str(value) if value else None


def effective_request_target(request: dict[str, Any] | None) -> tuple[float, float]:
    """Return (original, effective) user target, defensively capped at 9 kWh."""

    if not isinstance(request, dict):
        return SYNTHETIC_TARGET_KWH, SYNTHETIC_TARGET_KWH
    original = max(0.0, _float(
        request.get("requested_ac_kwh_original", request.get("required_ac_kwh")),
        SYNTHETIC_TARGET_KWH,
    ))
    return original, min(original, MAX_SESSION_KWH)


def notification_window(request: dict[str, Any] | None) -> tuple[str | None, str | None]:
    metadata = request.get("ev_schedule_notification") if isinstance(request, dict) else None
    if not isinstance(metadata, dict):
        return None, None
    start = metadata.get("last_notified_start") or metadata.get("initial_notified_start")
    end = metadata.get("last_notified_end") or metadata.get("initial_notified_end")
    return (str(start) if start else None, str(end) if end else None)


def idle_state(now: datetime, wallbox: dict[str, Any] | None = None) -> dict[str, Any]:
    wallbox = wallbox if isinstance(wallbox, dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "session_version": VERSION,
        "state": "IDLE",
        "measurement_available": bool(wallbox.get("available")),
        "measurement_source": wallbox.get("source") or "unavailable",
        "measurement_error": wallbox.get("error"),
        "current_power_w": wallbox.get("charging_power_w"),
        "updated_at": _iso(now),
        "replan_required": False,
        "replan_reason": None,
        "replan_claimed_at": None,
    }


def read_state(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_state(path: Path, state: dict[str, Any]) -> None:
    request_store.atomic_write_json(path, state)


def mark_completion_notification_sent(path: Path, *, session_id: str, now: datetime) -> dict[str, Any]:
    """Mark only the current matching session without reverting replan claims."""

    with request_store.request_store_lock(path):
        current = read_state(path)
        if current.get("session_id") != session_id:
            return current
        current["completion_notification_sent_at"] = _iso(now)
        write_state(path, current)
        return current


def _start_session(
    *,
    now: datetime,
    wallbox: dict[str, Any],
    active_ev_request: dict[str, Any] | None,
) -> dict[str, Any]:
    stamp = _iso(now)
    counter = max(0.0, _float(wallbox.get("charging_energy_kwh")))
    delivered = min(counter, MAX_SESSION_KWH)
    user_id = request_identity(active_ev_request)
    source = "user" if user_id else "synthetic"
    session_id = f"ev-{now.strftime('%Y%m%dT%H%M%S%z')}"
    request_id = user_id or f"synthetic:{session_id}"
    original, target = effective_request_target(active_ev_request)
    accepted_start, accepted_end = notification_window(active_ev_request)
    return {
        "schema_version": SCHEMA_VERSION,
        "session_version": VERSION,
        "session_id": session_id,
        "state": "ACTIVE",
        "request_id": request_id,
        "request_source": source,
        "requested_ac_kwh_original": round(original, 3),
        "effective_target_kwh": round(target, 3),
        "started_at": stamp,
        "last_active_at": stamp,
        "low_power_since": None,
        "paused_at": None,
        "closed_at": None,
        "wallbox_counter_start_kwh": round(counter, 3),
        "wallbox_counter_last_kwh": round(counter, 3),
        "wallbox_counter_raw_kwh": round(counter, 3),
        "delivered_kwh": round(delivered, 3),
        "request_credited_kwh": round(min(delivered, target), 3),
        "request_remaining_kwh": round(max(0.0, target - delivered), 3),
        "physical_remaining_to_max_kwh": round(max(0.0, MAX_SESSION_KWH - delivered), 3),
        "current_power_w": round(max(0.0, _float(wallbox.get("charging_power_w"))), 1),
        "measurement_available": True,
        "measurement_source": "wallbox_api",
        "measurement_error": None,
        "counter_reset_observed": False,
        "window_locked": True,
        "accepted_window_start": accepted_start,
        "accepted_window_end": accepted_end,
        "replan_required": True,
        "replan_reason": "EV_SESSION_STARTED",
        "replan_claimed_at": None,
        "updated_at": stamp,
    }


def _start_paused_session(
    *,
    now: datetime,
    wallbox: dict[str, Any],
    active_ev_request: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bootstrap an already-running physical session first seen in low-power tail."""

    state = _start_session(
        now=now, wallbox=wallbox, active_ev_request=active_ev_request
    )
    stamp = _iso(now)
    state.update({
        "state": "PAUSED",
        "last_active_at": None,
        "low_power_since": stamp,
        "paused_at": stamp,
        "bootstrap_from_low_power": True,
        "replan_reason": "EV_SESSION_DISCOVERED_PAUSED",
    })
    return state


def _update_energy(state: dict[str, Any], wallbox: dict[str, Any]) -> None:
    raw_counter = max(0.0, _float(wallbox.get("charging_energy_kwh")))
    previous = max(0.0, _float(state.get("delivered_kwh")))
    measured = min(raw_counter, MAX_SESSION_KWH)
    if measured + 0.05 < previous:
        state["counter_reset_observed"] = True
    delivered = max(previous, measured)
    target = min(MAX_SESSION_KWH, max(0.0, _float(state.get("effective_target_kwh"))))
    state["wallbox_counter_last_kwh"] = round(raw_counter, 3)
    state["wallbox_counter_raw_kwh"] = round(raw_counter, 3)
    state["delivered_kwh"] = round(delivered, 3)
    state["request_credited_kwh"] = round(min(delivered, target), 3)
    state["request_remaining_kwh"] = round(max(0.0, target - delivered), 3)
    state["physical_remaining_to_max_kwh"] = round(max(0.0, MAX_SESSION_KWH - delivered), 3)


def update_session(
    previous: dict[str, Any] | None,
    *,
    now: datetime,
    wallbox: dict[str, Any] | None,
    active_ev_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance one physical session from one direct wallbox sample.

    Unavailable API samples preserve an existing session and never advance its
    low-power timer. A continuous available low-power interval closes only when
    it is strictly longer than 30 minutes.
    """

    old = previous if isinstance(previous, dict) else {}
    wallbox = wallbox if isinstance(wallbox, dict) else {}
    old_state = str(old.get("state") or "IDLE")
    measurement_complete = (
        wallbox.get("available")
        and wallbox.get("charging_power_w") is not None
        and wallbox.get("charging_energy_kwh") is not None
    )
    if not measurement_complete:
        if old_state not in (*ACTIVE_STATES, "CLOSED"):
            state = idle_state(now, wallbox)
            state["measurement_available"] = False
            if wallbox.get("available"):
                state["measurement_error"] = "wallbox chargedata power/energy is incomplete"
            return state
        state = dict(old)
        state["measurement_available"] = False
        state["measurement_source"] = wallbox.get("source") or "unavailable"
        state["measurement_error"] = (
            wallbox.get("error")
            or "wallbox chargedata power/energy is incomplete"
        )
        state["current_power_w"] = None
        state["updated_at"] = _iso(now)
        return state

    power_w = max(0.0, _float(wallbox.get("charging_power_w")))
    counter_kwh = max(0.0, _float(wallbox.get("charging_energy_kwh")))
    is_active = power_w > ACTIVE_POWER_THRESHOLD_W
    if old_state == "CLOSED":
        if is_active:
            return _start_session(now=now, wallbox=wallbox, active_ev_request=active_ev_request)
        previous_raw = max(0.0, _float(old.get("wallbox_counter_raw_kwh")))
        if counter_kwh > 0.0 and counter_kwh + 0.05 < previous_raw:
            return _start_paused_session(
                now=now, wallbox=wallbox, active_ev_request=active_ev_request
            )
        state = dict(old)
        state["measurement_available"] = True
        state["measurement_source"] = "wallbox_api"
        state["measurement_error"] = None
        state["current_power_w"] = round(power_w, 1)
        state["updated_at"] = _iso(now)
        return state
    if old_state == "IDLE":
        if is_active:
            return _start_session(now=now, wallbox=wallbox, active_ev_request=active_ev_request)
        if counter_kwh > 0.0:
            return _start_paused_session(
                now=now, wallbox=wallbox, active_ev_request=active_ev_request
            )
        return idle_state(now, wallbox)

    state = dict(old)
    state["measurement_available"] = True
    state["measurement_source"] = "wallbox_api"
    state["measurement_error"] = None
    state["current_power_w"] = round(power_w, 1)
    state["updated_at"] = _iso(now)
    _update_energy(state, wallbox)

    if is_active:
        state["state"] = "ACTIVE"
        state["last_active_at"] = _iso(now)
        state["low_power_since"] = None
        state["paused_at"] = None
        state["closed_at"] = None
        state["window_locked"] = True
        return state

    low_since = _parse_datetime(state.get("low_power_since"), now)
    if low_since is None:
        low_since = now
        state["low_power_since"] = _iso(now)
        state["paused_at"] = _iso(now)
    if now - low_since <= timedelta(minutes=SESSION_END_GAP_MINUTES):
        state["state"] = "PAUSED"
        state["window_locked"] = True
        return state

    state["state"] = "CLOSED"
    state["closed_at"] = _iso(now)
    state["window_locked"] = False
    delivered = max(0.0, _float(state.get("delivered_kwh")))
    target = max(0.0, _float(state.get("effective_target_kwh")))
    deviation = round(delivered - target, 3)
    state["final_deviation_kwh"] = deviation
    if abs(deviation) >= REPLAN_DEVIATION_KWH:
        state["replan_required"] = True
        state["replan_reason"] = "EV_SESSION_CLOSED_DEVIATION"
        state["replan_claimed_at"] = None
    return state


def update_persisted_session(
    path: Path,
    *,
    now: datetime,
    wallbox: dict[str, Any] | None,
    active_ev_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with request_store.request_store_lock(path):
        state = update_session(
            read_state(path), now=now, wallbox=wallbox, active_ev_request=active_ev_request
        )
        write_state(path, state)
        return state


def claim_replan(path: Path, *, now: datetime) -> tuple[bool, dict[str, Any]]:
    """Atomically claim one pending replan; repeated executor runs return False."""

    with request_store.request_store_lock(path):
        state = read_state(path)
        if not state.get("replan_required") or state.get("replan_claimed_at"):
            return False, state
        state["replan_claimed_at"] = _iso(now)
        state["last_replan_reason"] = state.get("replan_reason")
        state["replan_required"] = False
        write_state(path, state)
        return True, state


def release_replan_claim(path: Path, *, reason: str | None = None) -> None:
    """Restore a claim after process-start failure so a later executor can retry."""

    with request_store.request_store_lock(path):
        state = read_state(path)
        if not state.get("replan_claimed_at"):
            return
        state["replan_required"] = True
        state["replan_reason"] = reason or state.get("last_replan_reason")
        state["replan_claimed_at"] = None
        write_state(path, state)
