"""Atomic storage helpers for planner_v10 user requests.

VERSION = "1.2"

Changelog:
- v1.2 (2026-07-27): Expire requests whose deadline/end has passed before
  listing or planning them.
- v1.1 (2026-07-24): Listing and cancellation helpers for WhatsApp commands.
- v1.0 (2026-07-23): Shared idempotent request store for WhatsApp/CLI inputs.

The planner already accepts `state/requests.json` either as a plain list or as
an object with a top-level `requests` key. This module writes the object form so
metadata can be added without breaking existing readers.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


VERSION = "1.2"


@dataclass(frozen=True)
class StoreResult:
    """Result of an idempotent request-store update."""

    stored: bool
    duplicate: bool
    request_id: str
    request_type: str
    replaced_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CancelResult:
    """Result of cancelling one active request by its displayed 1-based index."""

    canceled: bool
    display_id: int
    request_id: str | None = None
    request_type: str | None = None
    running: bool = False
    reason: str | None = None


def utc_now_iso() -> str:
    """Return a timezone-aware UTC timestamp for store metadata."""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON to `path` using a same-directory temp file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
        except (AttributeError, OSError):
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def read_request_document(path: Path) -> dict[str, Any]:
    """Read `requests.json`, tolerating the legacy list form."""

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        raw = []

    if isinstance(raw, dict):
        requests = raw.get("requests", [])
        if not isinstance(requests, list):
            requests = []
        doc = dict(raw)
        doc["requests"] = [item for item in requests if isinstance(item, dict)]
        return doc

    if isinstance(raw, list):
        return {
            "schema_version": 10,
            "requests": [item for item in raw if isinstance(item, dict)],
        }

    return {"schema_version": 10, "requests": []}


def _parse_datetime(value: Any, now: datetime) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=now.tzinfo)
    return parsed.astimezone(now.tzinfo)


def request_is_expired(item: dict[str, Any], *, now: datetime) -> bool:
    """Return whether an active request's inclusive purpose is wholly in the past."""
    request_type = item.get("type")
    time_key = "end" if request_type == "additional_load" else "deadline"
    boundary = _parse_datetime(item.get(time_key), now)
    return boundary is not None and boundary <= now


def expire_past_requests(path: Path, *, now: datetime | None = None) -> tuple[str, ...]:
    """Atomically mark past active requests as expired and return their IDs."""
    now = now or datetime.now().astimezone()
    doc = read_request_document(path)
    expired_ids: list[str] = []
    for item in doc["requests"]:
        if item.get("status", "active") != "active" or not request_is_expired(item, now=now):
            continue
        item["status"] = "expired"
        item["expired_at"] = now.isoformat(timespec="seconds")
        request_id = item.get("id") or item.get("request_id")
        if isinstance(request_id, str) and request_id:
            expired_ids.append(request_id)
    if expired_ids:
        doc["schema_version"] = 10
        doc["updated_at"] = now.isoformat(timespec="seconds")
        atomic_write_json(path, doc)
    return tuple(expired_ids)


def active_requests(path: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return active request objects in the stable display order from storage."""

    expire_past_requests(path, now=now)
    doc = read_request_document(path)
    return [
        item
        for item in doc.get("requests", [])
        if isinstance(item, dict) and item.get("status", "active") == "active"
    ]


def _request_identity(item: dict[str, Any]) -> set[str]:
    values = set()
    for key in ("request_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            values.add(value)
    return values


def store_request(
    path: Path,
    request: dict[str, Any],
    *,
    replace_existing_same_type: bool,
    updated_at: str | None = None,
) -> StoreResult:
    """Store one active user request idempotently.

    `request_id` is the idempotency key. For EV and boiler requests, callers set
    `replace_existing_same_type=True`, which marks older active requests of the
    same type as `replaced`. Additional loads pass `False` and can coexist.
    """

    request_id = str(request.get("request_id") or request.get("id") or "")
    request_type = str(request.get("type") or "")
    if not request_id:
        raise ValueError("request is missing request_id/id")
    if not request_type:
        raise ValueError("request is missing type")

    doc = read_request_document(path)
    requests = doc["requests"]
    identities = {request_id}
    identities.update(_request_identity(request))

    for item in requests:
        if identities.intersection(_request_identity(item)):
            return StoreResult(
                stored=False,
                duplicate=True,
                request_id=request_id,
                request_type=request_type,
            )

    stamp = updated_at or utc_now_iso()
    replaced_ids: list[str] = []
    if replace_existing_same_type:
        for item in requests:
            if item.get("type") == request_type and item.get("status", "active") == "active":
                item["status"] = "replaced"
                item["replaced_at"] = stamp
                item["replaced_by"] = request_id
                old_id = item.get("id") or item.get("request_id")
                if isinstance(old_id, str) and old_id:
                    replaced_ids.append(old_id)

    new_item = dict(request)
    new_item.setdefault("id", request_id)
    new_item.setdefault("request_id", request_id)
    new_item.setdefault("status", "active")
    requests.append(new_item)
    doc["schema_version"] = 10
    doc["updated_at"] = stamp
    atomic_write_json(path, doc)

    return StoreResult(
        stored=True,
        duplicate=False,
        request_id=request_id,
        request_type=request_type,
        replaced_ids=tuple(replaced_ids),
    )


def cancel_request_by_display_id(
    path: Path,
    display_id: int,
    *,
    running: bool = False,
    updated_at: str | None = None,
) -> CancelResult:
    """Cancel one active request by the 1-based id shown by the `requests` command."""

    if display_id < 1:
        return CancelResult(canceled=False, display_id=display_id, reason="invalid_id")

    doc = read_request_document(path)
    requests = doc["requests"]
    active_indices = [
        idx
        for idx, item in enumerate(requests)
        if isinstance(item, dict) and item.get("status", "active") == "active"
    ]
    if display_id > len(active_indices):
        return CancelResult(canceled=False, display_id=display_id, reason="not_found")

    item = requests[active_indices[display_id - 1]]
    stamp = updated_at or utc_now_iso()
    request_id = str(item.get("id") or item.get("request_id") or "")
    request_type = str(item.get("type") or "")
    item["status"] = "cancelled"
    item["cancelled_at"] = stamp
    item["cancelled_running"] = bool(running)
    doc["schema_version"] = 10
    doc["updated_at"] = stamp
    atomic_write_json(path, doc)

    return CancelResult(
        canceled=True,
        display_id=display_id,
        request_id=request_id or None,
        request_type=request_type or None,
        running=bool(running),
    )