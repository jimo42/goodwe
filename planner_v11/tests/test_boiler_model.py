"""Testy pro lib/boiler_model.py - hermetické.

Spouštět na serveru: cd planner_v10 && python3 tests/run_manual.py
(pytest NENÍ na serveru nainstalován - viz HANDOFF_v10.md).
"""
import copy
import os
import sys
import tomllib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import boiler_model as bm  # noqa: E402
from lib.config import parse_config_dict  # noqa: E402

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.toml")

_BASE_START = datetime(2026, 7, 21, 12, 0)


def _raw_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def _cfg(overrides: dict | None = None):
    raw = copy.deepcopy(_raw_config())
    if overrides:
        for section, kv in overrides.items():
            raw.setdefault(section, {}).update(kv)
    return parse_config_dict(raw)


def _slot_starts(n: int, cfg) -> list[datetime]:
    step = cfg.system.planning_step_minutes
    return [_BASE_START + timedelta(minutes=step * i) for i in range(n)]


def test_remaining_required_kwh_basic():
    cfg = _cfg()
    remaining = bm.remaining_required_kwh_for_hard_request(5.0, cfg)
    assert abs(remaining - (cfg.boiler.initial_full_heat_max_kwh - 5.0)) < 1e-9


def test_remaining_required_kwh_floors_at_zero():
    cfg = _cfg()
    remaining = bm.remaining_required_kwh_for_hard_request(1000.0, cfg)
    assert remaining == 0.0


def test_find_deadline_slot_idx_basic():
    cfg = _cfg()
    slot_starts = _slot_starts(10, cfg)
    step = cfg.system.planning_step_minutes
    # deadline uprostred slotu 3 (zacina slot_starts[3], konci pred slot_starts[4])
    deadline = slot_starts[3] + timedelta(minutes=step / 2)
    idx = bm.find_deadline_slot_idx(slot_starts, deadline, step)
    assert idx == 3


def test_find_deadline_slot_idx_before_first_slot_returns_none():
    cfg = _cfg()
    slot_starts = _slot_starts(10, cfg)
    deadline = slot_starts[0] - timedelta(minutes=1)
    idx = bm.find_deadline_slot_idx(slot_starts, deadline, cfg.system.planning_step_minutes)
    assert idx is None


def test_find_deadline_slot_idx_after_horizon_returns_none():
    cfg = _cfg()
    slot_starts = _slot_starts(10, cfg)
    step = cfg.system.planning_step_minutes
    horizon_end = slot_starts[-1] + timedelta(minutes=step)
    deadline = horizon_end + timedelta(minutes=1)
    idx = bm.find_deadline_slot_idx(slot_starts, deadline, step)
    assert idx is None


def test_find_deadline_slot_idx_exactly_at_horizon_end():
    cfg = _cfg()
    slot_starts = _slot_starts(10, cfg)
    step = cfg.system.planning_step_minutes
    horizon_end = slot_starts[-1] + timedelta(minutes=step)
    idx = bm.find_deadline_slot_idx(slot_starts, horizon_end, step)
    assert idx == len(slot_starts) - 1


def test_build_hard_request_basic():
    cfg = _cfg()
    slot_starts = _slot_starts(10, cfg)
    step = cfg.system.planning_step_minutes
    deadline = slot_starts[3] + timedelta(minutes=step / 2)
    req = bm.build_hard_request(slot_starts, deadline, already_delivered_kwh=5.0, cfg=cfg)
    assert req is not None
    assert req.deadline_idx == 3
    assert abs(req.required_kwh - (cfg.boiler.initial_full_heat_max_kwh - 5.0)) < 1e-9


def test_build_hard_request_none_when_deadline_outside_horizon():
    cfg = _cfg()
    slot_starts = _slot_starts(5, cfg)
    deadline = slot_starts[0] - timedelta(hours=1)
    req = bm.build_hard_request(slot_starts, deadline, already_delivered_kwh=0.0, cfg=cfg)
    assert req is None


def test_build_hard_request_none_when_fully_delivered():
    cfg = _cfg()
    slot_starts = _slot_starts(10, cfg)
    deadline = slot_starts[5]
    req = bm.build_hard_request(
        slot_starts, deadline, already_delivered_kwh=cfg.boiler.initial_full_heat_max_kwh, cfg=cfg
    )
    assert req is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
