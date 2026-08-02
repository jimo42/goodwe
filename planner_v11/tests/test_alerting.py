"""Hermetic tests for planner_v10 notification deduplication."""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import alerting  # noqa: E402


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="planner_v10_test_alerting_")


def test_notify_once_deduplicates_same_message_within_repeat_window():
    tmp = _tmpdir()
    original = alerting.notify.send
    calls = []
    try:
        state_path = Path(tmp) / "alert_state.json"
        alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
        now = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)

        first = alerting.notify_once("k", "message", state_path=state_path, now=now, repeat_minutes=60)
        second = alerting.notify_once("k", "message", state_path=state_path, now=now + timedelta(minutes=10), repeat_minutes=60)

        assert first["sent"] is True
        assert second["sent"] is False
        assert second["reason"] == "deduplicated"
        assert calls == ["message"]
        doc = json.loads(state_path.read_text(encoding="utf-8"))
        assert doc["alerts"]["k"]["send_count"] == 1
    finally:
        alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_notify_once_repeats_after_window():
    tmp = _tmpdir()
    original = alerting.notify.send
    calls = []
    try:
        state_path = Path(tmp) / "alert_state.json"
        alerting.notify.send = lambda message, **kwargs: calls.append(message) or True
        now = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)

        alerting.notify_once("k", "message", state_path=state_path, now=now, repeat_minutes=60)
        second = alerting.notify_once("k", "message", state_path=state_path, now=now + timedelta(minutes=61), repeat_minutes=60)

        assert second["sent"] is True
        assert calls == ["message", "message"]
    finally:
        alerting.notify.send = original
        shutil.rmtree(tmp, ignore_errors=True)