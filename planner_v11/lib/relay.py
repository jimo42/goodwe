"""
Relay adapter for boiler phases in planner_v10.

VERSION = "1.1"

Changelog:
- v1.1 (2026-08-02): Support arbitrary phase masks, OFF-before-ON switching,
  final exact-mask read-back, and verify writes even after an empty response.
- v1.0 (2026-07-22): Low-level relay read/write helpers copied from the
  already verified v8 adapter pattern, including read-back verification after
  each real write.

Verified source pattern from the old relay scripts / v8 adapter:
- command ports: phase1 OFF=16 ON=17, phase2 OFF=18 ON=19,
  phase3 OFF=20 ON=21, pump OFF=02 ON=03,
- status uses `GET /98`, parsed as a 4-character bitfield where positions
  4=pump, 3=phase1, 2=phase2, 1=phase3 (1-indexed from the left).
"""
from __future__ import annotations

import os
import socket
import time
from typing import Any, Optional

from . import paths


VERSION = "1.1"

RELAY_CONF_PATH = os.path.join(paths.BASE_DIR, "conf", "relay.conf")


def _get_relay_ip(path: str = RELAY_CONF_PATH) -> str:
    """Parse the bash-style relay config (`IP_ADDRESS=...`)."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("IP_ADDRESS="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"IP_ADDRESS not found in {path}")


def _port_for(phase: str, state: str) -> str:
    phase = phase.upper()
    state = state.upper()
    if phase == "C":
        return "03" if state == "ON" else "02"
    phase_num = int(phase)
    base = (phase_num + 7) * 2
    return str(base + 1) if state == "ON" else str(base)


def _http_get(path: str, ip: str, timeout: float = 5.0) -> Optional[str]:
    """Read relay's short non-compliant HTTP response without strict framing.

    The relay serves the expected four status bytes for `/98`, but advertises a
    larger HTTP Content-Length. `requests` treats this as an incomplete response
    and raises `ChunkedEncodingError`; curl (used by the established relay
    scripts) accepts the bytes. Read directly to preserve the verified device
    protocol while still using a bounded timeout.
    """
    try:
        with socket.create_connection((ip, 8080), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall(
                f"GET /{path} HTTP/1.0\r\nHost: {ip}\r\nConnection: close\r\n\r\n".encode("ascii")
            )
            chunks: list[bytes] = []
            while True:
                try:
                    chunk = conn.recv(1024)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks)
        body = raw.split(b"\r\n\r\n", 1)[-1]
        text = body.decode("ascii", errors="replace").strip()
        return text or None
    except OSError:
        return None


def read_status_pole() -> Optional[str]:
    """Read the 4-character relay status bitfield via `GET /98`."""
    ip = _get_relay_ip()
    return _http_get("98", ip)


def parse_pole(pole: Optional[str]) -> dict[str, Optional[bool]]:
    if not pole or len(pole) < 4:
        return {"pump": None, "phase1": None, "phase2": None, "phase3": None}

    def bit_at(pos_1indexed: int) -> bool:
        return pole[pos_1indexed - 1] != "0"

    return {
        "pump": bit_at(4),
        "phase1": bit_at(3),
        "phase2": bit_at(2),
        "phase3": bit_at(1),
    }


def running_phase_count(pole: Optional[str]) -> Optional[int]:
    parsed = parse_pole(pole)
    values = [parsed["phase1"], parsed["phase2"], parsed["phase3"]]
    if any(v is None for v in values):
        return None
    return sum(1 for v in values if v)


def set_phase(phase: str, state: str, *, dry_run: bool = True, verify_delay_s: float = 1.0) -> dict[str, Any]:
    """Set one phase/pump relay and verify by reading `/98` after the write."""
    phase = phase.upper()
    state = state.upper()
    if phase not in ("1", "2", "3", "C"):
        raise ValueError(f"Invalid relay phase: {phase}")
    if state not in ("ON", "OFF"):
        raise ValueError(f"Invalid relay state: {state}")

    ip = _get_relay_ip()
    before_pole = read_status_pole()
    before_parsed = parse_pole(before_pole)
    result: dict[str, Any] = {
        "adapter_version": VERSION,
        "phase": phase,
        "state": state,
        "dry_run": dry_run,
        "before_pole": before_pole,
        "before_parsed": before_parsed,
    }

    if dry_run:
        result["status"] = "dry_run_skipped"
        result["relay_status_ok"] = None
        return result

    port = _port_for(phase, state)
    response = _http_get(port, ip)
    result["http_port"] = port
    result["http_response"] = response
    time.sleep(verify_delay_s)
    after_pole = read_status_pole()
    after_parsed = parse_pole(after_pole)
    result["after_pole"] = after_pole
    result["after_parsed"] = after_parsed
    if after_pole is None:
        result["status"] = "verify_read_failed"
        result["relay_status_ok"] = False
        return result

    key = {"1": "phase1", "2": "phase2", "3": "phase3", "C": "pump"}[phase]
    expected = state == "ON"
    actual = after_parsed.get(key)
    result["verified"] = actual == expected
    result["relay_status_ok"] = True
    result["command_response_missing"] = response is None
    result["status"] = "written_verified_by_readback" if result["verified"] else "write_verification_failed"
    return result


def apply_phase_target(target_phases: int, *, dry_run: bool = True, verify_delay_s: float = 1.0) -> dict[str, Any]:
    """Apply a 0..3 boiler phase target using per-phase verified writes."""
    if target_phases < 0 or target_phases > 3:
        raise ValueError(f"target_phases must be 0..3, got {target_phases}")

    return apply_phase_mask(
        tuple(phase_num <= target_phases for phase_num in (1, 2, 3)),
        dry_run=dry_run,
        verify_delay_s=verify_delay_s,
    )


def mask_from_parsed(parsed: dict[str, Optional[bool]]) -> Optional[tuple[bool, bool, bool]]:
    values = tuple(parsed.get(f"phase{i}") for i in (1, 2, 3))
    if any(value is None for value in values):
        return None
    return tuple(bool(value) for value in values)


def apply_phase_mask(target_mask: tuple[bool, bool, bool], *, dry_run: bool = True, verify_delay_s: float = 1.0) -> dict[str, Any]:
    """Apply an arbitrary phase mask, removing phases before adding new ones."""
    if len(target_mask) != 3 or any(not isinstance(value, bool) for value in target_mask):
        raise ValueError(f"target_mask must contain three bools, got {target_mask!r}")
    current_pole = read_status_pole()
    parsed = parse_pole(current_pole)
    current_mask = mask_from_parsed(parsed)
    result: dict[str, Any] = {
        "adapter_version": VERSION,
        "target_phases": sum(target_mask),
        "target_mask": list(target_mask),
        "dry_run": dry_run,
        "before_pole": current_pole,
        "before_parsed": parsed,
        "actions": [],
    }
    if current_mask is None:
        result["status"] = "status_read_failed"
        result["relay_status_ok"] = False
        return result

    changes = [
        (phase_num, "OFF")
        for phase_num in (1, 2, 3)
        if current_mask[phase_num - 1] and not target_mask[phase_num - 1]
    ] + [
        (phase_num, "ON")
        for phase_num in (1, 2, 3)
        if not current_mask[phase_num - 1] and target_mask[phase_num - 1]
    ]
    for phase_num, state in changes:
        action = set_phase(str(phase_num), state, dry_run=dry_run, verify_delay_s=verify_delay_s)
        result["actions"].append(action)
        if action.get("verified") is False or action.get("relay_status_ok") is False:
            break

    if not result["actions"]:
        result["status"] = "already_at_target"
        result["relay_status_ok"] = True
        return result

    if dry_run:
        result["relay_status_ok"] = all(action.get("relay_status_ok") is not False for action in result["actions"])
        result["status"] = "written" if result["relay_status_ok"] else "write_verification_failed"
        return result
    final_pole = read_status_pole()
    final_mask = mask_from_parsed(parse_pole(final_pole))
    result["after_pole"] = final_pole
    result["after_mask"] = list(final_mask) if final_mask is not None else None
    result["verified"] = final_mask == target_mask
    result["relay_status_ok"] = final_mask is not None
    result["status"] = "written" if result["verified"] else "write_verification_failed"
    return result