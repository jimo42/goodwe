"""Testy pro lib/ev_model.py - hermetické.

`evaluate_fn` je ve všech testech FAKE (nevolá skutečný solver) - testuje
se čistě logika enumerace/rankingu/výběru v tomto modulu, ne MILP samotné
(to kryje tests/test_optimizer.py).

Spouštět na serveru: cd planner_v10 && python3 tests/run_manual.py
(pytest NENÍ na serveru nainstalován - viz HANDOFF_v10.md).
"""
import copy
import os
import sys
import tomllib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import ev_model as em  # noqa: E402
from lib.config import parse_config_dict  # noqa: E402
from lib.optimizer import OptimizerResult  # noqa: E402

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


def test_enumerate_candidates_basic_window():
    cfg = _cfg()
    slot_starts = _slot_starts(20, cfg)
    available_from = slot_starts[2]
    # required_ac_kwh malé, aby stačil 1 slot (planning_power_kw*0.25 > required)
    required = cfg.ev.planning_power_kw * 0.25 * 0.5
    deadline = slot_starts[10]
    prices = [50.0] * 20
    candidates = em.enumerate_candidates(
        slot_starts, available_from, deadline, required, cfg, prices
    )
    assert len(candidates) > 0
    # zadny kandidat nezacina pred available_from
    assert all(c.start_time >= available_from for c in candidates)
    # zadny kandidat nekonci po deadline - taper_reserve
    taper = timedelta(minutes=cfg.ev.taper_reserve_minutes)
    assert all(c.end_time <= deadline - taper for c in candidates)


def test_enumerate_candidates_empty_when_deadline_too_close():
    cfg = _cfg()
    slot_starts = _slot_starts(10, cfg)
    available_from = slot_starts[0]
    deadline = slot_starts[0] + timedelta(minutes=1)  # prakticky zadny prostor
    required = 10.0  # velky pozadavek
    prices = [50.0] * 10
    candidates = em.enumerate_candidates(
        slot_starts, available_from, deadline, required, cfg, prices
    )
    assert candidates == []


def test_rank_candidates_sorts_by_cheap_score():
    candidates = [
        em.EvCandidate(0, 0, _BASE_START, _BASE_START, cheap_score_czk_per_kwh=5.0),
        em.EvCandidate(1, 1, _BASE_START, _BASE_START, cheap_score_czk_per_kwh=1.0),
        em.EvCandidate(2, 2, _BASE_START, _BASE_START, cheap_score_czk_per_kwh=3.0),
    ]
    ranked = em.rank_candidates(candidates)
    assert [c.cheap_score_czk_per_kwh for c in ranked] == [1.0, 3.0, 5.0]


def test_evaluate_candidates_picks_lowest_economic_objective():
    cfg = _cfg()
    candidates = [
        em.EvCandidate(0, 0, _BASE_START, _BASE_START, cheap_score_czk_per_kwh=1.0),
        em.EvCandidate(1, 1, _BASE_START, _BASE_START, cheap_score_czk_per_kwh=2.0),
    ]

    def fake_evaluate(req):
        # kandidat s window_start_idx=1 ma nizsi ekonomicky objective
        if req.window_start_idx == 0:
            return OptimizerResult(status="optimal", ev_unserved_kwh=0.0, economic_objective_czk=10.0)
        return OptimizerResult(status="optimal", ev_unserved_kwh=0.0, economic_objective_czk=2.0)

    best_candidate, best_result = em.evaluate_candidates(
        candidates, required_ac_kwh=1.0, cfg=cfg, evaluate_fn=fake_evaluate
    )
    assert best_candidate.start_idx == 1
    assert best_result.economic_objective_czk == 2.0


def test_evaluate_candidates_skips_infeasible_ev_unserved():
    cfg = _cfg()
    candidates = [
        em.EvCandidate(0, 0, _BASE_START, _BASE_START, cheap_score_czk_per_kwh=1.0),
        em.EvCandidate(1, 1, _BASE_START, _BASE_START, cheap_score_czk_per_kwh=2.0),
    ]

    def fake_evaluate(req):
        if req.window_start_idx == 0:
            return OptimizerResult(status="optimal", ev_unserved_kwh=0.5, economic_objective_czk=1.0)
        return OptimizerResult(status="optimal", ev_unserved_kwh=0.0, economic_objective_czk=5.0)

    best_candidate, best_result = em.evaluate_candidates(
        candidates, required_ac_kwh=1.0, cfg=cfg, evaluate_fn=fake_evaluate
    )
    assert best_candidate.start_idx == 1


def test_latest_safe_start_basic():
    cfg = _cfg()
    deadline = _BASE_START
    required = cfg.ev.planning_power_kw * 2  # 2 hodiny potreba
    safe_start = em.latest_safe_start(deadline, required, cfg)
    expected = deadline - timedelta(hours=2) - timedelta(minutes=cfg.ev.taper_reserve_minutes)
    assert safe_start == expected


def test_recommend_feasible_end_to_end():
    cfg = _cfg()
    slot_starts = _slot_starts(30, cfg)
    available_from = slot_starts[0]
    deadline = slot_starts[25]
    required = cfg.ev.planning_power_kw * 0.25  # 1 slot staci
    prices = [100.0 - i for i in range(30)]  # klesajici cena, levnejsi pozdeji

    def fake_evaluate(req):
        return OptimizerResult(status="optimal", ev_unserved_kwh=0.0, economic_objective_czk=1.0)

    rec = em.recommend(
        slot_starts, available_from, deadline, required, cfg, prices, fake_evaluate
    )
    assert rec.feasible is True
    assert rec.recommended_start is not None
    assert rec.expected_delivered_kwh == required


def test_recommend_infeasible_when_no_candidates():
    cfg = _cfg()
    slot_starts = _slot_starts(5, cfg)
    available_from = slot_starts[0]
    deadline = slot_starts[0] + timedelta(minutes=1)
    required = 20.0
    prices = [100.0] * 5

    def fake_evaluate(req):
        return OptimizerResult(status="optimal", ev_unserved_kwh=0.0, economic_objective_czk=1.0)

    rec = em.recommend(
        slot_starts, available_from, deadline, required, cfg, prices, fake_evaluate
    )
    assert rec.feasible is False
    assert rec.recommended_start is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
