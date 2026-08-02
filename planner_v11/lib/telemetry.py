"""Recent minute telemetry used by the five-minute boiler controller.

VERSION = "1.1"

Changelog:
- v1.1 (2026-08-02): Deterministic replay age, local parsers, persisted
  OFF-baseline support and confirmed-delivery surplus reconstruction.
- v1.0 (2026-08-02): Parse standard GoodWe minute reports, combine them with
  the current live read and expose robust export and phase-load evidence.
"""
from __future__ import annotations

import glob
import os
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from . import load_model, paths

VERSION = "1.1"
_STANDARD_REPORT_RE = re.compile(r"^goodwe_stats_\d{8}_\d{6}$")


def _parse_float(value: str) -> Optional[float]:
    try:
        return float(value.split()[0])
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def _parse_timestamp(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except (AttributeError, TypeError, ValueError):
        return None


def _number(raw: dict[str, str], key: str) -> Optional[float]:
    value = _parse_float(raw.get(key, ""))
    return None if value is None else value / 1000.0


@dataclass(frozen=True)
class MinuteSample:
    timestamp: datetime
    pv_kw: float
    house_kw: float
    export_kw: float
    phase_load_kw: tuple[float, float, float]
    phase_export_kw: tuple[float, float, float]
    source: str


def parse_report(path: str, tz=None) -> Optional[MinuteSample]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            raw = load_model.parse_report_text(handle.read())
    except OSError:
        return None
    timestamp = _parse_timestamp(raw.get("timestamp", ""))
    values = {
        key: _number(raw, key)
        for key in (
            "ppv1", "ppv2", "house_consumption", "meter_active_power_total",
            "load_p1", "load_p2", "load_p3", "meter_active_power1",
            "meter_active_power2", "meter_active_power3",
        )
    }
    if timestamp is None or any(values[key] is None for key in values):
        return None
    if tz is not None:
        timestamp = timestamp.replace(tzinfo=tz)
    return MinuteSample(
        timestamp=timestamp,
        pv_kw=float(values["ppv1"] + values["ppv2"]),
        house_kw=float(values["house_consumption"]),
        export_kw=float(values["meter_active_power_total"]),
        phase_load_kw=tuple(float(values[f"load_p{i}"]) for i in (1, 2, 3)),
        phase_export_kw=tuple(float(values[f"meter_active_power{i}"]) for i in (1, 2, 3)),
        source="minute_report",
    )


def sample_from_live(live_state: dict, now: datetime) -> Optional[MinuteSample]:
    try:
        return MinuteSample(
            timestamp=now,
            pv_kw=(float(live_state.get("ppv1", 0) or 0) + float(live_state.get("ppv2", 0) or 0)) / 1000.0,
            house_kw=float(live_state["house_consumption"]) / 1000.0,
            export_kw=float(live_state["meter_active_power_total"]) / 1000.0,
            phase_load_kw=tuple(float(live_state[f"load_p{i}"]) / 1000.0 for i in (1, 2, 3)),
            phase_export_kw=tuple(float(live_state[f"meter_active_power{i}"]) / 1000.0 for i in (1, 2, 3)),
            source="live_read",
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_recent_samples(
    *, now: datetime, live_state: dict, since: Optional[datetime] = None,
    reports_dir: str = paths.GOODWE_REPORTS_DIR, lookback_minutes: float = 6.0,
) -> list[MinuteSample]:
    cutoff = since or (now - timedelta(minutes=lookback_minutes))
    samples: list[MinuteSample] = []
    for path in glob.glob(os.path.join(reports_dir, "goodwe_stats_*")):
        if not _STANDARD_REPORT_RE.match(os.path.basename(path)):
            continue
        sample = parse_report(path, now.tzinfo)
        if sample is not None and cutoff < sample.timestamp <= now:
            samples.append(sample)
    live = sample_from_live(live_state, now)
    if live is not None:
        samples.append(live)
    samples.sort(key=lambda item: item.timestamp)
    return samples


def robust_evidence(
    samples: list[MinuteSample],
    current_mask: tuple[bool, bool, bool],
    phase_power_kw: float,
    *,
    now: Optional[datetime] = None,
    persisted_phase_baseline_kw: Optional[list[Optional[float]]] = None,
) -> dict[str, Any]:
    if not samples:
        return {"sample_count": 0, "status": "TELEMETRY_UNAVAILABLE"}
    exports = [sample.export_kw for sample in samples]
    latest = exports[-1]
    median = statistics.median(exports)
    stable = min(latest, median)
    persisted = persisted_phase_baseline_kw if isinstance(persisted_phase_baseline_kw, list) and len(persisted_phase_baseline_kw) == 3 else [None, None, None]
    phase_baseline = []
    confirmed_phase_delivery_kw = []
    for phase_idx in range(3):
        values = [sample.phase_load_kw[phase_idx] for sample in samples]
        observed = statistics.median(values)
        stored_baseline = persisted[phase_idx]
        if current_mask[phase_idx] and stored_baseline is not None:
            baseline = max(0.0, float(stored_baseline))
            confirmed = min(phase_power_kw, max(0.0, observed - baseline))
        elif current_mask[phase_idx]:
            # A relay bit alone is not proof of heat delivery. Until an OFF-state
            # baseline exists, expose no confirmed boiler power.
            baseline = max(0.0, observed - phase_power_kw)
            confirmed = 0.0
        else:
            baseline = max(0.0, observed)
            confirmed = 0.0
        phase_baseline.append(baseline)
        confirmed_phase_delivery_kw.append(confirmed)
    confirmed_kw = sum(confirmed_phase_delivery_kw)
    reference_now = now or datetime.now(samples[-1].timestamp.tzinfo)
    return {
        "status": "OK",
        "sample_count": len(samples),
        "oldest_at": samples[0].timestamp.isoformat(),
        "latest_at": samples[-1].timestamp.isoformat(),
        "latest_age_seconds": max(0.0, (reference_now - samples[-1].timestamp).total_seconds()),
        "export_min_kw": round(min(exports), 6),
        "export_median_kw": round(median, 6),
        "export_latest_kw": round(latest, 6),
        "stable_export_kw": round(stable, 6),
        "reconstructed_pre_boiler_surplus_kw": round(stable + confirmed_kw, 6),
        "confirmed_boiler_delivery_kw": round(confirmed_kw, 6),
        "confirmed_phase_delivery_kw": [round(value, 6) for value in confirmed_phase_delivery_kw],
        "robust_phase_baseline_kw": [round(value, 6) for value in phase_baseline],
    }