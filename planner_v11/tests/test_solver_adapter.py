"""Testy pro lib/solver_adapter.py - triviální LP/MILP scénáře.

Spouštět na serveru: cd planner_v10 && python3 tests/run_manual.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pulp  # noqa: E402

from lib import solver_adapter as sa  # noqa: E402


def test_solve_simple_optimal():
    prob = pulp.LpProblem("t", pulp.LpMinimize)
    x = pulp.LpVariable("x", lowBound=0)
    prob += x >= 3
    prob += x
    result = sa.solve(prob, time_limit_seconds=5.0, mip_gap=0.001)
    assert result.is_optimal
    assert result.status == sa.STATUS_OPTIMAL
    assert abs(x.value() - 3.0) < 1e-4
    assert abs(result.objective_value - 3.0) < 1e-4
    assert result.solver_name == "pulp_cbc"


def test_solve_infeasible():
    prob = pulp.LpProblem("t", pulp.LpMinimize)
    x = pulp.LpVariable("x", lowBound=0, upBound=1)
    prob += x >= 5
    prob += x
    result = sa.solve(prob, time_limit_seconds=5.0, mip_gap=0.001)
    assert not result.is_optimal
    assert result.status == sa.STATUS_INFEASIBLE
    assert result.objective_value is None


def test_solve_milp_with_binary():
    prob = pulp.LpProblem("t", pulp.LpMinimize)
    b = pulp.LpVariable("b", cat=pulp.LpBinary)
    prob += b >= 0.5  # vynutí b == 1
    prob += b
    result = sa.solve(prob, time_limit_seconds=5.0, mip_gap=0.001)
    assert result.is_optimal
    assert round(b.value()) == 1


def test_integer_feasible_solution_is_not_reported_as_optimal():
    prob = pulp.LpProblem("t", pulp.LpMinimize)
    x = pulp.LpVariable("x", lowBound=0)
    prob += x
    prob.status = pulp.LpStatusOptimal
    prob.sol_status = pulp.LpSolutionIntegerFeasible

    original_solve = pulp.LpProblem.solve
    pulp.LpProblem.solve = lambda self, solver=None, **kwargs: self.status
    try:
        result = sa.solve(prob, time_limit_seconds=5.0, mip_gap=0.001)
    finally:
        pulp.LpProblem.solve = original_solve

    assert not result.is_optimal
    assert result.status == sa.STATUS_FEASIBLE


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
