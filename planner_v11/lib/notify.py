"""
Notification wrapper for planner_v10.

VERSION = "1.0"

Changelog:
- v1.0 (2026-07-24): Send UTF-8 text to the server-side
  `/home/automatization/goodwe/bin/notify_admins.sh` helper via stdin.

The helper script is an existing server contract documented in
WHATSAPP_AUTOMATION.md. This module is intentionally tiny and has no knowledge
of alert semantics or deduplication; `lib.alerting` owns that layer.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


VERSION = "1.0"
DEFAULT_NOTIFY_SCRIPT = Path("/home/automatization/goodwe/bin/notify_admins.sh")


def send(
    message: str,
    *,
    notify_script: Path | str = DEFAULT_NOTIFY_SCRIPT,
    timeout_seconds: float = 15.0,
    dry_run: bool = False,
) -> bool:
    """Send `message` to admins. Returns True only when helper exits with 0."""

    text = str(message).rstrip() + "\n"
    if dry_run:
        print(f"[DRY_RUN notify_admins] {text.rstrip()}")
        return True

    try:
        result = subprocess.run(
            [str(notify_script)],
            input=text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"CHYBA: notify_admins.sh selhalo: {exc}", file=sys.stderr)
        return False

    if result.returncode != 0:
        print(
            "CHYBA: notify_admins.sh vratil "
            f"{result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}",
            file=sys.stderr,
        )
        return False
    return True