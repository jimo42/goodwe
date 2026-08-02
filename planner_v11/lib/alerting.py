"""
Deduplicated admin alerts for planner_v10.

VERSION = "1.0"

Changelog:
- v1.0 (2026-07-24): Add JSON-backed deduplication over `notify_admins.sh`
  for fault alerts and daily report messages.

State file contract:
  state/alert_state.json

This module never writes to devices. It only writes its own deduplication state
and optionally calls the configured notification helper.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import notify


VERSION = "1.0"
PLANNER_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = PLANNER_DIR / "state"
DEFAULT_ALERT_STATE_PATH = STATE_DIR / "alert_state.json"


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read_state(path: Path = DEFAULT_ALERT_STATE_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 10, "alerts": {}}
    if not isinstance(raw, dict):
        return {"schema_version": 10, "alerts": {}}
    alerts = raw.get("alerts")
    if not isinstance(alerts, dict):
        raw["alerts"] = {}
    raw.setdefault("schema_version", 10)
    return raw


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


def repeat_minutes_from_cfg(cfg: Any, default: float = 60.0) -> float:
    try:
        return float(cfg.alerts.fault_repeat_minutes)
    except (AttributeError, TypeError, ValueError):
        return default


def notify_once(
    key: str,
    message: str,
    *,
    cfg: Any | None = None,
    state_path: Path = DEFAULT_ALERT_STATE_PATH,
    notify_script: Path | str = notify.DEFAULT_NOTIFY_SCRIPT,
    now: datetime | None = None,
    repeat_minutes: float | None = None,
    force: bool = False,
) -> dict:
    """Send a deduplicated alert and persist last-send metadata."""

    now = now or datetime.now().astimezone()
    repeat = repeat_minutes if repeat_minutes is not None else repeat_minutes_from_cfg(cfg)
    repeat_delta = timedelta(minutes=max(0.0, float(repeat)))

    state = read_state(state_path)
    alerts = state.setdefault("alerts", {})
    existing = alerts.get(key) if isinstance(alerts.get(key), dict) else {}
    last_sent = _parse_dt(existing.get("last_sent_at")) if isinstance(existing, dict) else None
    if (
        not force
        and last_sent is not None
        and now - last_sent < repeat_delta
        and existing.get("last_message") == message
    ):
        return {"sent": False, "reason": "deduplicated", "key": key, "last_sent_at": last_sent.isoformat()}

    ok = notify.send(message, notify_script=notify_script)
    alerts[key] = {
        "last_seen_at": now.isoformat(),
        "last_message": message,
        "last_send_ok": ok,
        "send_count": int(existing.get("send_count", 0) if isinstance(existing, dict) else 0) + (1 if ok else 0),
    }
    if ok:
        alerts[key]["last_sent_at"] = now.isoformat()
    elif isinstance(existing, dict) and existing.get("last_sent_at"):
        alerts[key]["last_sent_at"] = existing.get("last_sent_at")
    atomic_write_json(state_path, state)
    return {"sent": ok, "reason": "sent" if ok else "send_failed", "key": key}


def notify_daily(
    key: str,
    message: str,
    *,
    state_path: Path = DEFAULT_ALERT_STATE_PATH,
    notify_script: Path | str = notify.DEFAULT_NOTIFY_SCRIPT,
    now: datetime | None = None,
    force: bool = False,
) -> dict:
    """Send a daily message at most once per key unless forced."""

    return notify_once(
        key,
        message,
        state_path=state_path,
        notify_script=notify_script,
        now=now,
        repeat_minutes=24 * 60,
        force=force,
    )