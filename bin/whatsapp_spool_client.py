#!/usr/bin/env python3
"""Client for the durable WhatsApp outgoing spool.

This file contains no WAHA URL, API key, or WhatsApp group ID.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

SPOOL = Path("/home/automatization/goodwe/whatsapp-spool")
OUTGOING = SPOOL / "outgoing"
OUTGOING_DONE = SPOOL / "outgoing-done"
OUTGOING_FAILED = SPOOL / "outgoing-failed"
MAX_BYTES = 4096
SCHEMA = 1


def fail(message: str, code: int = 1) -> None:
    print(f"CHYBA: {message}", file=sys.stderr)
    raise SystemExit(code)


def read_stdin() -> str:
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if not data:
        fail("prázdná zpráva", 64)
    if len(data) > MAX_BYTES:
        fail(f"zpráva je delší než {MAX_BYTES} B", 64)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        fail("zpráva není platné UTF-8", 64)
    if not text.strip():
        fail("prázdná zpráva", 64)
    return text.strip()


def atomic_write(filename: str, value: Mapping[str, Any]) -> Path:
    OUTGOING.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=OUTGOING)
    temp = Path(temp_name)
    final = OUTGOING / filename
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o660)
        os.replace(temp, final)
        return final
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def load_request(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    try:
        resolved = path.resolve(strict=True)
        processing = (SPOOL / "processing").resolve(strict=True)
        if resolved.parent != processing:
            fail("request musí ležet přímo v adresáři processing", 64)
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"request nelze načíst: {exc}", 64)
    if not isinstance(value, dict) or value.get("schema") != 1:
        fail("request má nepodporovaný formát", 64)
    return value


def wait_for_result(filename: str) -> int:
    raw_timeout = os.getenv("WAHA_QUEUE_WAIT", os.getenv("WAHA_ACK_TIMEOUT", "3"))
    try:
        timeout = max(0.0, float(raw_timeout))
    except ValueError:
        fail("WAHA_QUEUE_WAIT musí být číslo", 64)
    deadline = time.monotonic() + timeout
    done = OUTGOING_DONE / filename
    failed = OUTGOING_FAILED / filename
    while time.monotonic() < deadline:
        if done.exists():
            print("sent")
            return 0
        if failed.exists():
            try:
                data = json.loads(failed.read_text(encoding="utf-8"))
                error = data.get("delivery", {}).get("error") or data.get("last_error")
            except Exception:
                error = None
            fail(str(error or "odeslání selhalo"), 1)
        time.sleep(0.1)
    print("odesílání odloženo")
    return 0


def enqueue(item: dict[str, Any]) -> int:
    queue_id = str(uuid.uuid4())
    item.update(
        {
            "schema": SCHEMA,
            "queue_id": queue_id,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "attempts": 0,
        }
    )
    filename = f"{datetime.now().strftime('%Y%m%dT%H%M%S.%f')}-out-{queue_id}.json"
    try:
        atomic_write(filename, item)
    except OSError as exc:
        fail(f"zprávu nelze zařadit do fronty: {exc}", 75)
    return wait_for_result(filename)


def command_notify(_args: argparse.Namespace) -> int:
    return enqueue(
        {
            "origin": "automation-notification",
            "text": read_stdin(),
        }
    )


def command_reply(args: argparse.Namespace) -> int:
    request = load_request(args.request)
    source = request.get("source")
    if not isinstance(source, dict):
        fail("request neobsahuje source", 64)
    message_id = source.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        fail("request neobsahuje source.message_id", 64)
    item: dict[str, Any] = {
        "origin": "automation-reply",
        "text": read_stdin(),
        "request_id": request.get("request_id"),
        "reply_to_message_id": message_id,
    }
    sender = source.get("sender_id")
    if isinstance(sender, str) and sender:
        item["participant"] = sender
    return enqueue(item)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    notify = subparsers.add_parser("notify")
    notify.set_defaults(func=command_notify)
    reply = subparsers.add_parser("reply")
    reply.add_argument("request")
    reply.set_defaults(func=command_reply)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
