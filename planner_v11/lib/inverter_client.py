"""
GoodWe adapter helpers for planner_v10.

VERSION = "1.1"

Changelog:
- v1.0 (2026-07-22): Low-level ECO schedule encoder/writer copied from the
  already verified v8 adapter pattern. The module does not decide *when* to
  write; it only provides a guarded 12-byte ECO write with read-back check.

Safety rules documented in SERVER_ACCESS.md and verified in the previous v8
adapter still apply:
- never write `eco_mode_N_switch` separately,
- always write the full 12-byte `eco_mode_N` block,
- after a real write, read the same setting back and verify at least the
  requested on/off state.
"""
from __future__ import annotations

import configparser
import os
import struct
import sys
from typing import Any

from . import paths


VERSION = "1.1"

GOODWE_LIB_DIR = os.path.join(paths.BASE_DIR, "goodwe", "goodwe")
GOODWE_CONF_PATH = os.path.join(paths.BASE_DIR, "conf", "goodwe.conf")

DAY_BIT = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
ALL_DAYS = list(DAY_BIT.keys())


def _get_ip_address(path: str = GOODWE_CONF_PATH) -> str:
    cfg = configparser.ConfigParser()
    read_files = cfg.read(path)
    if not read_files:
        raise RuntimeError(f"GoodWe config file not found: {path}")
    return cfg["settings"]["ip_address"]


async def connect():
    """Connect to GoodWe using the locally vendored goodwe library."""
    if GOODWE_LIB_DIR not in sys.path:
        sys.path.insert(0, GOODWE_LIB_DIR)
    import goodwe  # noqa: PLC0415

    return await goodwe.connect(_get_ip_address())


async def read_runtime_data() -> dict[str, Any]:
    """Read all runtime sensors into a flat `{sensor_id: value}` dictionary."""
    inverter = await connect()
    data = await inverter.read_runtime_data()
    out: dict[str, Any] = {}
    for sensor in inverter.sensors():
        if sensor.id_ in data:
            out[sensor.id_] = data[sensor.id_]
    return out


def encode_schedule(
    start_h: int,
    start_m: int,
    end_h: int,
    end_m: int,
    day_names: list[str],
    power_pct: int,
    soc_pct: int,
    enabled: bool,
) -> bytes:
    """Encode one GoodWe ECO schedule group as the verified 12-byte block."""
    day_bits = 0
    for day in day_names:
        day_bits |= 1 << DAY_BIT[day]
    on_off = -1 if enabled else 0
    return struct.pack(">BBBBbBhhh", start_h, start_m, end_h, end_m, on_off, day_bits, power_pct, soc_pct, 0)


async def write_eco_mode(
    channel: int,
    start_h: int,
    start_m: int,
    end_h: int,
    end_m: int,
    day_names: list[str],
    power_pct: int,
    soc_pct: int,
    enabled: bool,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Write one `eco_mode_N` schedule and verify it by reading back.

    With `dry_run=True`, no connection/write is attempted; the function only
    returns the exact raw payload that would be written.
    """
    if channel not in (1, 2, 3, 4):
        raise ValueError("channel must be 1-4")

    raw = encode_schedule(start_h, start_m, end_h, end_m, day_names, power_pct, soc_pct, enabled)
    name = f"eco_mode_{channel}"
    result: dict[str, Any] = {
        "adapter_version": VERSION,
        "channel": channel,
        "name": name,
        "raw_hex": raw.hex(),
        "requested": {
            "start": f"{start_h:02d}:{start_m:02d}",
            "end": f"{end_h:02d}:{end_m:02d}",
            "days": list(day_names),
            "power_pct": power_pct,
            "soc_pct": soc_pct,
            "enabled": enabled,
        },
        "dry_run": dry_run,
    }

    if dry_run:
        result["status"] = "dry_run_skipped"
        return result

    inverter = await connect()
    before = await inverter.read_setting(name)
    result["before"] = repr(before)

    await inverter.write_setting(name, raw)

    after = await inverter.read_setting(name)
    result["after"] = repr(after)

    expected_on_off = -1 if enabled else 0
    actual_on_off = getattr(after, "on_off", None)
    result["verified"] = actual_on_off == expected_on_off
    result["status"] = "written" if result["verified"] else "write_verification_failed"
    return result


async def read_eco_mode(channel: int):
    if channel not in (1, 2, 3, 4):
        raise ValueError("channel must be 1-4")
    inverter = await connect()
    return await inverter.read_setting(f"eco_mode_{channel}")


def _schedule_matches(actual: Any, requested: dict[str, Any]) -> bool:
    """Verify all meaningful fields of one ECO schedule after a write."""
    expected_day_bits = 0
    for day in requested["day_names"]:
        expected_day_bits |= 1 << DAY_BIT[day]
    return (
        getattr(actual, "start_h", None) == requested["start_h"]
        and getattr(actual, "start_m", None) == requested["start_m"]
        and getattr(actual, "end_h", None) == requested["end_h"]
        and getattr(actual, "end_m", None) == requested["end_m"]
        and getattr(actual, "on_off", None) == (-1 if requested["enabled"] else 0)
        and getattr(actual, "day_bits", None) == expected_day_bits
        and getattr(actual, "power", None) == requested["power_pct"]
        and getattr(actual, "soc", None) == requested["soc_pct"]
    )


async def write_eco_modes(
    schedules: list[dict[str, Any]], *, dry_run: bool = True
) -> dict[str, Any]:
    """Write the complete four-channel ECO plan and verify every block.

    GoodWe exposes each channel as a separate Modbus setting, but v10 owns the
    *entire* four-window plan. The helper therefore reads all channels first,
    writes all requested complete 12-byte blocks on one connection, and then
    reads all four back. It never writes the unsafe ``*_switch`` aliases.
    """
    if len(schedules) != 4:
        raise ValueError("exactly four ECO schedules are required")
    channels = [item.get("channel") for item in schedules]
    if sorted(channels) != [1, 2, 3, 4]:
        raise ValueError("schedules must contain channels 1, 2, 3 and 4 exactly once")

    normalized: list[dict[str, Any]] = []
    for source in sorted(schedules, key=lambda item: int(item["channel"])):
        item = {
            "channel": int(source["channel"]),
            "start_h": int(source["start_h"]),
            "start_m": int(source["start_m"]),
            "end_h": int(source["end_h"]),
            "end_m": int(source["end_m"]),
            "day_names": list(source["day_names"]),
            "power_pct": int(source["power_pct"]),
            "soc_pct": int(source["soc_pct"]),
            "enabled": bool(source["enabled"]),
        }
        if not (0 <= item["start_h"] <= 23 and 0 <= item["end_h"] <= 23
                and 0 <= item["start_m"] <= 59 and 0 <= item["end_m"] <= 59):
            raise ValueError("invalid ECO time")
        if not (-100 <= item["power_pct"] <= 100 and 0 <= item["soc_pct"] <= 100):
            raise ValueError("invalid ECO power or SoC")
        if any(day not in DAY_BIT for day in item["day_names"]):
            raise ValueError("invalid ECO day")
        item["name"] = f"eco_mode_{item['channel']}"
        item["raw_hex"] = encode_schedule(
            item["start_h"], item["start_m"], item["end_h"], item["end_m"],
            item["day_names"], item["power_pct"], item["soc_pct"], item["enabled"],
        ).hex()
        normalized.append(item)

    result: dict[str, Any] = {
        "adapter_version": VERSION,
        "dry_run": dry_run,
        "schedules": normalized,
    }
    if dry_run:
        result["status"] = "dry_run_skipped"
        return result

    inverter = await connect()
    if GOODWE_LIB_DIR not in sys.path:
        sys.path.insert(0, GOODWE_LIB_DIR)
    import goodwe  # noqa: PLC0415
    before = {}
    for item in normalized:
        before[item["name"]] = repr(await inverter.read_setting(item["name"]))
    result["before"] = before

    for item in normalized:
        raw = bytes.fromhex(item["raw_hex"])
        await inverter.write_setting(item["name"], raw)

    # Unlike ECO_CHARGE/ECO_DISCHARGE, the general ECO operation mode only
    # selects work_mode=3 and does not overwrite the four custom schedules.
    result["operation_mode_before"] = repr(await inverter.get_operation_mode())
    await inverter.set_operation_mode(goodwe.OperationMode.ECO)
    result["operation_mode_after"] = repr(await inverter.get_operation_mode())

    after = {}
    verified = True
    for item in normalized:
        actual = await inverter.read_setting(item["name"])
        after[item["name"]] = repr(actual)
        item["verified"] = _schedule_matches(actual, item)
        verified = verified and item["verified"]
    result["after"] = after
    result["verified"] = verified and result["operation_mode_after"].find("ECO") >= 0
    result["status"] = "written" if result["verified"] else "write_verification_failed"
    return result