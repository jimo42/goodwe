"""Hermetic tests for lib.request_store."""
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lib import request_store


def _tmpdir():
    return tempfile.mkdtemp(prefix="planner_v10_test_request_store_")


def _read(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_store_ev_replaces_existing_active_ev_request():
    tmp = _tmpdir()
    try:
        path = Path(tmp) / "requests.json"
        first = {
            "id": "old",
            "request_id": "old",
            "type": "ev_charge",
            "status": "active",
            "required_ac_kwh": 5.0,
            "deadline": "2026-07-23T08:30:00+02:00",
        }
        second = {
            "id": "new",
            "request_id": "new",
            "type": "ev_charge",
            "status": "active",
            "required_ac_kwh": 8.0,
            "deadline": "2026-07-23T09:30:00+02:00",
        }
        request_store.store_request(path, first, replace_existing_same_type=True, updated_at="2026-07-23T00:00:00+02:00")
        result = request_store.store_request(path, second, replace_existing_same_type=True, updated_at="2026-07-23T00:01:00+02:00")
        doc = _read(path)
        assert result.stored is True
        assert result.replaced_ids == ("old",)
        assert doc["requests"][0]["status"] == "replaced"
        assert doc["requests"][0]["replaced_by"] == "new"
        assert doc["requests"][1]["status"] == "active"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_store_additional_loads_allow_multiple_active_items():
    tmp = _tmpdir()
    try:
        path = Path(tmp) / "requests.json"
        for idx in (1, 2):
            request_store.store_request(
                path,
                {
                    "id": f"load-{idx}",
                    "request_id": f"load-{idx}",
                    "type": "additional_load",
                    "status": "active",
                    "power_kw": idx,
                    "start": "2026-07-23T10:00:00+02:00",
                    "end": "2026-07-23T11:00:00+02:00",
                },
                replace_existing_same_type=False,
            )
        doc = _read(path)
        assert len(doc["requests"]) == 2
        assert [item["status"] for item in doc["requests"]] == ["active", "active"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_store_request_is_idempotent_by_request_id():
    tmp = _tmpdir()
    try:
        path = Path(tmp) / "requests.json"
        req = {
            "id": "same",
            "request_id": "same",
            "type": "boiler_full",
            "status": "active",
            "deadline": "2026-07-23T08:30:00+02:00",
        }
        first = request_store.store_request(path, req, replace_existing_same_type=True)
        duplicate = request_store.store_request(path, dict(req), replace_existing_same_type=True)
        doc = _read(path)
        assert first.stored is True
        assert duplicate.duplicate is True
        assert len(doc["requests"]) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cancel_request_by_display_id_marks_active_item_cancelled():
    tmp = _tmpdir()
    try:
        path = Path(tmp) / "requests.json"
        for idx in (1, 2):
            request_store.store_request(
                path,
                {
                    "id": f"req-{idx}",
                    "request_id": f"req-{idx}",
                    "type": "additional_load",
                    "status": "active",
                    "power_kw": idx,
                },
                replace_existing_same_type=False,
            )
        result = request_store.cancel_request_by_display_id(
            path,
            2,
            running=True,
            updated_at="2026-07-24T12:00:00+02:00",
        )
        doc = _read(path)
        assert result.canceled is True
        assert result.request_id == "req-2"
        assert result.running is True
        assert doc["requests"][0]["status"] == "active"
        assert doc["requests"][1]["status"] == "cancelled"
        assert doc["requests"][1]["cancelled_running"] is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_active_requests_returns_only_active_items():
    tmp = _tmpdir()
    try:
        path = Path(tmp) / "requests.json"
        path.write_text(
            json.dumps({
                "schema_version": 10,
                "requests": [
                    {"id": "active", "type": "ev_charge", "status": "active"},
                    {"id": "cancelled", "type": "ev_charge", "status": "cancelled"},
                ],
            }),
            encoding="utf-8",
        )
        active = request_store.active_requests(path)
        assert [item["id"] for item in active] == ["active"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_active_requests_expires_past_deadline_and_end():
    tmp = _tmpdir()
    try:
        path = Path(tmp) / "requests.json"
        path.write_text(
            json.dumps({
                "schema_version": 10,
                "requests": [
                    {"id": "old-ev", "type": "ev_charge", "status": "active", "deadline": "2026-07-26T12:00:00+02:00"},
                    {"id": "old-load", "type": "additional_load", "status": "active", "end": "2026-07-26T12:00:00+02:00"},
                    {"id": "future", "type": "ev_charge", "status": "active", "deadline": "2026-07-28T12:00:00+02:00"},
                ],
            }),
            encoding="utf-8",
        )
        now = datetime(2026, 7, 27, 12, 0, tzinfo=ZoneInfo("Europe/Prague"))
        active = request_store.active_requests(path, now=now)
        doc = _read(path)
        assert [item["id"] for item in active] == ["future"]
        assert doc["requests"][0]["status"] == "expired"
        assert doc["requests"][1]["status"] == "expired"
        assert "expired_at" in doc["requests"][0]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)