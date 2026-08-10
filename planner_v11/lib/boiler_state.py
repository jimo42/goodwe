"""Atomic boiler command/delivery ledger shared by planner and executor.

VERSION = "1.2"

Changelog:
- v1.2 (2026-08-10): Persist daily thermostat-stop observation and completion
  notification metadata alongside the existing delivery ledger.
- v1.1 (2026-08-02): Split accounting at local midnight and cap stale
  accounting intervals while retaining commanded/delivered separation.
- v1.0 (2026-08-02): Track daily commanded and estimated delivered energy,
  relay masks, transition timestamps and baseline confidence.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any, Optional

from .telemetry import MinuteSample

VERSION = "1.2"


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "adapter_version": VERSION,
        "previous_executor_at": None,
        "current_mask": [False, False, False],
        "phase_last_changed_at": [None, None, None],
        "phase_last_on_at": [None, None, None],
        "phase_last_off_at": [None, None, None],
        "phase_baseline_kw": [None, None, None],
        "days": {},
    }


def normalize_state(raw: Any) -> dict[str, Any]:
    state = empty_state()
    if isinstance(raw, dict):
        state.update(raw)
    if not isinstance(state.get("days"), dict):
        state["days"] = {}
    for key in ("current_mask", "phase_last_changed_at", "phase_last_on_at", "phase_last_off_at", "phase_baseline_kw"):
        if not isinstance(state.get(key), list) or len(state[key]) != 3:
            state[key] = empty_state()[key]
    return state


def today_entry(state: dict, local_date) -> dict:
    key = local_date.isoformat()
    days = state.setdefault("days", {})
    entry = days.setdefault(key, {
        "commanded_kwh": 0.0,
        "estimated_delivered_kwh": 0.0,
        "delivery_confidence": "none",
        "delivery_source": "no_samples",
        "previous_confirmed_delivery_kw": 0.0,
        "full_detected_at": None,
        "full_notification_sent_at": None,
    })
    entry.setdefault("previous_confirmed_delivery_kw", 0.0)
    entry.setdefault("full_detected_at", None)
    entry.setdefault("full_notification_sent_at", None)
    return entry


def _interval_segments(previous: Optional[datetime], now: datetime) -> list[tuple[datetime, datetime]]:
    """Split at local midnight and bound stale/missed accounting to one hour."""
    if previous is None or previous >= now:
        return []
    cursor = max(previous, now - timedelta(hours=1))
    segments = []
    while cursor < now:
        next_midnight = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = min(now, next_midnight)
        segments.append((cursor, end))
        cursor = end
    return segments


def update_energy_and_baselines(
    state: dict, *, now: datetime, samples: list[MinuteSample], relay_mask: tuple[bool, bool, bool], phase_power_kw: float,
) -> dict:
    state = normalize_state(state)
    previous_raw = state.get("previous_executor_at")
    try:
        previous = datetime.fromisoformat(previous_raw) if previous_raw else None
        if previous is not None and previous.tzinfo is None:
            previous = previous.replace(tzinfo=now.tzinfo)
    except (TypeError, ValueError):
        previous = None
    baselines = list(state.get("phase_baseline_kw", [None, None, None]))
    for idx in range(3):
        if not relay_mask[idx] and samples:
            baselines[idx] = round(statistics.median(sample.phase_load_kw[idx] for sample in samples), 6)

    segments = _interval_segments(previous, now)
    last_interval = None
    for segment_start, segment_end in segments:
        elapsed_h = (segment_end - segment_start).total_seconds() / 3600.0
        segment_samples = [sample for sample in samples if segment_start < sample.timestamp <= segment_end]
        delivered_kw_samples: list[float] = []
        known_phase_observations = 0
        for sample in segment_samples:
            delivered_kw = 0.0
            known = False
            for idx, enabled in enumerate(relay_mask):
                if not enabled or baselines[idx] is None:
                    continue
                known = True
                known_phase_observations += 1
                delivered_kw += min(phase_power_kw, max(0.0, sample.phase_load_kw[idx] - float(baselines[idx])))
            if known:
                delivered_kw_samples.append(delivered_kw)
        estimated_kwh = statistics.mean(delivered_kw_samples) * elapsed_h if delivered_kw_samples else 0.0
        commanded_kwh = elapsed_h * phase_power_kw * sum(relay_mask)
        entry = today_entry(state, segment_start.date())
        entry["commanded_kwh"] = round(float(entry.get("commanded_kwh", 0.0) or 0.0) + commanded_kwh, 6)
        entry["estimated_delivered_kwh"] = round(float(entry.get("estimated_delivered_kwh", 0.0) or 0.0) + estimated_kwh, 6)
        entry["delivery_confidence"] = "high" if len(delivered_kw_samples) >= 3 else ("medium" if delivered_kw_samples else "low")
        entry["delivery_source"] = "relay_mask_plus_phase_load_delta" if delivered_kw_samples else "baseline_unavailable_no_delivery_assumed"
        last_interval = {
            "start": segment_start.isoformat(), "end": segment_end.isoformat(), "hours": round(elapsed_h, 6),
            "relay_mask": list(relay_mask), "sample_count": len(segment_samples),
            "known_phase_observations": known_phase_observations,
            "commanded_kwh": round(commanded_kwh, 6), "estimated_delivered_kwh": round(estimated_kwh, 6),
        }
        entry["last_interval"] = last_interval
    state["phase_baseline_kw"] = baselines
    state["previous_executor_at"] = now.isoformat()
    state["current_mask"] = list(relay_mask)
    state["updated_at"] = now.isoformat()
    state["last_accounting_interval"] = last_interval
    return state


def record_mask_transition(state: dict, *, old_mask: tuple[bool, bool, bool], new_mask: tuple[bool, bool, bool], now: datetime) -> dict:
    state = normalize_state(state)
    for idx, (old, new) in enumerate(zip(old_mask, new_mask)):
        if old == new:
            continue
        state["phase_last_changed_at"][idx] = now.isoformat()
        state["phase_last_on_at" if new else "phase_last_off_at"][idx] = now.isoformat()
    state["current_mask"] = list(new_mask)
    state["updated_at"] = now.isoformat()
    return state
