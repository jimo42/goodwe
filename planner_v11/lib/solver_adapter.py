"""
Tenká abstrakce nad MILP solverem pro planner v10.

Účel (ARCHITECTURE_DESIGN_v10_FINAL.md sekce 10.1, SERVER_IMPLEMENTATION_GUIDE_v10.md
sekce 11.1): obchodní logika (lib/optimizer.py) NESMÍ záviset na textových
vlastnostech konkrétního solveru (např. přímo porovnávat `pulp.LpStatus`
stringy jako "Optimal"/"Infeasible"). Tento modul normalizuje status do
vlastního uzavřeného výčtu (STATUS_* konstanty), takže výměna solveru
(scipy.optimize.milp / OR-Tools) by v budoucnu vyžadovala změnu jen tady.

Zvolený solver pro v1: PuLP + CBC (`PULP_CBC_CMD`).

Zdůvodnění volby (viz HANDOFF_v10.md, ověřeno přímo na serveru):
  - PuLP 2.7.0 a CBC jsou nainstalované SYSTÉMOVĚ na serveru a funkční
    (viz _tmp_check_solvers.py smoke test) - žádná další instalace přes pip
    není nutná (server je "externally-managed", PEP 668).
  - CBC je deterministický při stejných vstupech (žádná randomizace),
    podporuje časový limit (`timeLimit`) i relativní MIP gap (`gapRel`) -
    obojí požadováno v config.toml [solver].
  - Řeší diskrétní bojlerové fáze (binární proměnné) i zákazy směrů
    (charge/discharge, import/export) - vyžaduje MILP, ne LP (sekce 10.1).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pulp

# Normalizovaný status - business logika (lib/optimizer.py) porovnává JEN
# tyto konstanty, NIKDY přímo pulp.LpStatus stringy.
STATUS_OPTIMAL = "optimal"
STATUS_FEASIBLE = "feasible"
STATUS_INFEASIBLE = "infeasible"
STATUS_UNBOUNDED = "unbounded"
STATUS_NOT_SOLVED = "not_solved"
STATUS_UNKNOWN = "unknown"

_PULP_STATUS_MAP = {
    "Optimal": STATUS_OPTIMAL,
    "Infeasible": STATUS_INFEASIBLE,
    "Unbounded": STATUS_UNBOUNDED,
    "Not Solved": STATUS_NOT_SOLVED,
    "Undefined": STATUS_UNKNOWN,
}


@dataclass(frozen=True)
class SolveResult:
    status: str
    objective_value: Optional[float]
    solver_name: str

    @property
    def is_optimal(self) -> bool:
        return self.status == STATUS_OPTIMAL


def solve(
    problem: "pulp.LpProblem",
    time_limit_seconds: float = 30.0,
    mip_gap: float = 0.001,
    msg: bool = False,
) -> SolveResult:
    """Vyřeší MILP problém (sestavený volajícím přes PuLP) a vrátí
    normalizovaný SolveResult. Sám o sobě NEPROVÁDÍ žádnou obchodní logiku -
    jen spustí solver a přeloží jeho status/objective do neutrálního tvaru.

    `problem` je mutován solverem (standardní PuLP chování - hodnoty
    proměnných jsou po volání dostupné přes `variable.value()`)."""
    solver = pulp.PULP_CBC_CMD(msg=msg, timeLimit=time_limit_seconds, gapRel=mip_gap)
    problem.solve(solver)
    pulp_status = pulp.LpStatus[problem.status]
    status = _PULP_STATUS_MAP.get(pulp_status, STATUS_UNKNOWN)
    # PuLP 2.7 maps a CBC time-limit stop with an incumbent to the generic
    # problem status "Optimal", but keeps the more precise solution status as
    # "Solution Found" (LpSolutionIntegerFeasible). Do not promote that
    # unproven incumbent to an optimum: the optimizer must be able to fall back
    # to the previous lexicographic stage instead of publishing stage-3 values.
    solution_status = getattr(problem, "sol_status", None)
    if status == STATUS_OPTIMAL and solution_status == pulp.LpSolutionIntegerFeasible:
        status = STATUS_FEASIBLE
    objective_value = None
    if status in (STATUS_OPTIMAL, STATUS_FEASIBLE) and problem.objective is not None:
        objective_value = pulp.value(problem.objective)
    return SolveResult(status=status, objective_value=objective_value, solver_name="pulp_cbc")
