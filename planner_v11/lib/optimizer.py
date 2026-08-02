"""
Jádro MILP optimalizace pro planner v10 - jeden souvislý 15minutový horizont.

Autoritativní zdroj pravidel:
  - ARCHITECTURE_DESIGN_v10_FINAL.md sekce 9 (priority), 10 (optimalizační
    model), 11 (bateriové akce).
  - CONTROL_LOGIC_SPEC_v10.yaml sekce `optimizer`.
  - SERVER_IMPLEMENTATION_GUIDE_v10.md sekce 11 (solver a implementace).

Rozsah tohoto modulu:
  - Sestaví a vyřeší MILP nad N sloty (typicky 192 = 48h/15min) pro toky
    energie PV/síť/baterie/bojler/EV podle proměnných z ARCHITECTURE_DESIGN
    sekce 10.2-10.6.
  - Řeší LEXIKOGRAFICKY (sekce 9, 10.7, SERVER_IMPLEMENTATION_GUIDE_v10.md
    sekce 11.2) - NE jednou obří vahovou funkcí:
        stage 1: minimalizovat kritické sloty (EV/bojler hard deadline slack)
        stage 2: fixovat stage 1 na optimum, minimalizovat ekonomiku (CZK)
        stage 3: fixovat stage 2 s tolerancí (`economic_tie_tolerance_czk`),
                 minimalizovat bateriový throughput (částečná úroveň 3 -
                 mode changes/relay switches jsou mimo rozsah tohoto modulu,
                 řeší executor).
  - VŠECHNY ekonomické vzorce/konstanty jde přes `lib/economics.py` - žádná
    duplikace zde.
  - Cena importu/exportu, PV predikce a zda je export povolen jsou
    PŘEDPOČÍTANÉ vstupy (SlotInput) - tento modul neřeší price/weather
    estimation (to je `lib/prices.py`/`lib/weather.py`), jen bere hodnoty.
  - EV: podle sekce 10.2 "wallbox není akční člen" - volající už VYBRAL
    jeden kandidátní souvislý interval (enumerace mimo tento modul, viz
    budoucí `lib/ev_model.py`) a předá ho jako `EvRequest` s rozsahem
    indexů slotů a požadovanou energií; tento modul jen řeší, kolik z toho
    se skutečně (ekonomicky) dodá v rámci toho intervalu a slack při
    nedostatku (fyzická nemožnost).

Vědomá zjednodušení pro v1 (dokumentovaná, ne skryté zkratky):
  - SoC model BEZE ZVLÁŠTNÍ účinnosti (nominálně 1:1), viz ARCH sekce 10.4 -
    každý běh plannera se znovu ukotví na live SoC.
  - "Celkový výkon střídače ≤10 kW" (ARCH sekce 10.5) je aproximováno jako
    souhrn AC toků (grid_import + grid_export + battery charge/discharge
    energie) v daném slotu - přesné rozdělení PV-invertoru a battery-
    invertoru nebylo v architektuře specifikováno do detailu.
  - Fázové headroom omezení (ARCH sekce 10.6) se v tomto MILP NEŘEŠÍ -
    "Executor je autoritativní bezpečnostní vrstva" pro fáze v reálném čase,
    optimizer pracuje na úrovni celkové energie/výkonu.
  - Úroveň 3 cílové funkce je zde jen bateriový throughput tie-break; počet
    přepnutí režimu/relé a odchylka od předchozího plánu se řeší až v
    `planner.py`/`executor.py` (mimo rozsah čistého MILP jádra).

Boiler hard vs. oportunistický ohřev (2026-07-25, HANDOFF požadavek uživatele):
  - `boiler_hard_energy[t]`/`boiler_opportunistic_energy[t]` rozdělují každou
    dodanou bojlerovou energii na dvě kategorie. Hard energie smí téct JEN do
    slotů před `boiler_hard_request.deadline_idx` (pevná rovnost na
    `required_kwh`, žádný přebytek nad rámec hard požadavku se do "hard"
    nepočítá). Zbytek je oportunistický ohřev.
  - Oportunistický ohřev je nadále řízen JEN ekonomikou stage 2 (gas_heat_czk_kwh
    v `economic_objective` zůstává jediným zdrojem "vyplatí se topit" signálu -
    žádný nový bonus/penalizace zde nepřibyl). `boiler_opportunistic_limit_kwh`
    (výchozí `cfg.boiler.initial_full_heat_max_kwh`, tj. 15 kWh) je jen HORNÍ
    konzervativní strop na součet oportunistické energie za celý horizont, aby
    MILP nemohl navrhnout neomezené "potenciální" oportunistické ohřevy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

import pulp

from . import economics, solver_adapter

if TYPE_CHECKING:
    from .config import Config

_EPS = 1e-6


# ============================================================================
# Vstupní/výstupní datové struktury
# ============================================================================

@dataclass(frozen=True)
class SlotInput:
    """Jeden 15min plánovací slot - všechny předpočítané vstupy pro MILP.

    `fixed_load_kwh` MUSÍ obsahovat základní spotřebu domu + bazén +
    ohlášené dodatečné zátěže (ARCH sekce 7.1) - NESMÍ obsahovat EV (řeší
    se samostatně přes `EvRequest`, viz ARCH sekce 10.2) ani bojler (ten je
    v tomto modelu samostatně řízenou proměnnou)."""

    slot_start: datetime
    price_import_czk_kwh: float
    price_export_czk_kwh: float
    export_allowed: bool
    effective_import_nonpositive: bool
    pv_kwh: float
    fixed_load_kwh: float


@dataclass(frozen=True)
class EvRequest:
    """Jeden already-vybraný kandidátní souvislý interval pro nabíjení auta
    (enumerace kandidátů je mimo tento modul, viz ARCH sekce 10.2/8.1)."""

    window_start_idx: int
    window_end_idx: int  # inclusive
    required_ac_kwh: float
    planning_power_kw: float


@dataclass(frozen=True)
class BoilerHardRequest:
    """Tvrdý požadavek na nahřátí bojleru do daného slotu (ARCH sekce 8.2)."""

    deadline_idx: int  # inclusive - poslední slot, do kterého musí být dodáno
    required_kwh: float


@dataclass
class SlotResult:
    slot_start: datetime
    pv_to_fixed_load_kwh: float
    pv_to_boiler_kwh: float
    pv_to_battery_kwh: float
    pv_to_grid_kwh: float
    pv_curtailed_kwh: float
    grid_to_fixed_load_kwh: float
    grid_to_boiler_kwh: float
    grid_to_battery_kwh: float
    battery_to_fixed_load_kwh: float
    battery_to_boiler_kwh: float
    battery_to_grid_kwh: float
    soc_start_kwh: float
    soc_end_kwh: float
    boiler_phase_on: tuple  # (bool, bool, bool, ...) délka cfg.boiler.phase_count
    ev_delivered_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_action: str
    boiler_hard_kwh: float = 0.0
    boiler_opportunistic_kwh: float = 0.0


@dataclass
class OptimizerResult:
    status: str
    slots: list = field(default_factory=list)  # list[SlotResult]
    ev_unserved_kwh: float = 0.0
    boiler_hard_unserved_kwh: float = 0.0
    economic_objective_czk: float = 0.0
    terminal_soc_kwh: float = 0.0


# Battery action labely (ARCH sekce 11 / CONTROL_LOGIC_SPEC_v10.yaml battery.actions)
ACTION_SELF_USE = "SELF_USE"
ACTION_HOLD = "HOLD"
ACTION_FORCE_CHARGE = "FORCE_CHARGE"
ACTION_DISCHARGE_TO_LOAD = "DISCHARGE_TO_LOAD"
ACTION_DISCHARGE_TO_GRID = "DISCHARGE_TO_GRID"


def _classify_battery_action(
    grid_to_battery: float,
    battery_to_grid: float,
    battery_to_fixed_load: float,
    battery_to_boiler: float,
    grid_to_fixed_load: float,
    grid_to_boiler: float,
    soc_kwh: float,
    min_soc_kwh: float,
) -> str:
    """Odvodí popisný `battery_action` label pro daný slot z výsledných
    toků (jen pro vysvětlitelnost/`reason_codes` - NEOVLIVŇUJE MILP)."""
    if grid_to_battery > _EPS:
        return ACTION_FORCE_CHARGE
    if battery_to_grid > _EPS:
        return ACTION_DISCHARGE_TO_GRID
    if battery_to_fixed_load > _EPS or battery_to_boiler > _EPS:
        return ACTION_DISCHARGE_TO_LOAD
    # baterie nevydává ani nepřijímá ze sítě energii v tomto slotu.
    deficit_covered_by_grid = grid_to_fixed_load > _EPS or grid_to_boiler > _EPS
    if deficit_covered_by_grid and soc_kwh > min_soc_kwh + _EPS:
        return ACTION_HOLD
    return ACTION_SELF_USE


# ============================================================================
# Hlavní vstupní bod
# ============================================================================

def optimize(
    slots: list,
    cfg: "Config",
    initial_soc_kwh: float,
    terminal_value_czk_per_kwh: float = 0.0,
    ev_request: Optional[EvRequest] = None,
    boiler_hard_request: Optional[BoilerHardRequest] = None,
    boiler_opportunistic_limit_kwh: Optional[float] = None,
    boiler_opportunistic_daily_limits_kwh: Optional[dict[date, float]] = None,
) -> OptimizerResult:
    """Sestaví a lexikograficky vyřeší MILP nad `slots` (list[SlotInput]).

    `initial_soc_kwh` je live SoC baterie v kWh na začátku horizontu
    (planner se při každém běhu znovu ukotví na skutečném SoC, ARCH 10.4).
    `terminal_value_czk_per_kwh` je předpočítaná konzervativní hodnota
    energie zbylé v baterii na konci horizontu (viz `lib.economics` a ARCH
    sekce 6.4 - výpočet samotné hodnoty je mimo rozsah tohoto modulu)."""
    n = len(slots)
    if n == 0:
        return OptimizerResult(status=solver_adapter.STATUS_INFEASIBLE)

    slot_hours = cfg.system.planning_step_minutes / 60.0
    b = cfg.battery
    capacity_kwh = b.capacity_kwh
    min_soc_kwh = b.min_soc_pct / 100.0 * capacity_kwh
    max_soc_pv_kwh = b.max_soc_pv_pct / 100.0 * capacity_kwh
    max_soc_grid_kwh = b.max_soc_grid_pct / 100.0 * capacity_kwh
    max_charge_energy = b.max_charge_kw * slot_hours
    max_discharge_energy = b.max_discharge_kw * slot_hours
    max_grid_energy = cfg.grid.inverter_total_kw * slot_hours
    phase_count = cfg.boiler.phase_count
    max_boiler_energy = phase_count * cfg.boiler.phase_power_kw * slot_hours
    battery_cycle_czk_kwh = economics.battery_cycle_cost_czk_per_kwh(cfg)
    gas_heat_czk_kwh = economics.gas_heat_value_czk_per_kwh(cfg)

    prob = pulp.LpProblem("planner_v10", pulp.LpMinimize)

    # --- Proměnné (ARCH sekce 10.2) -----------------------------------
    idx = range(n)
    pv_to_fixed_load = pulp.LpVariable.dicts("pv_to_fixed_load", idx, lowBound=0)
    pv_to_boiler = pulp.LpVariable.dicts("pv_to_boiler", idx, lowBound=0)
    pv_to_battery = pulp.LpVariable.dicts("pv_to_battery", idx, lowBound=0)
    pv_to_grid = pulp.LpVariable.dicts("pv_to_grid", idx, lowBound=0)
    pv_curtailed = pulp.LpVariable.dicts("pv_curtailed", idx, lowBound=0)

    grid_to_fixed_load = pulp.LpVariable.dicts("grid_to_fixed_load", idx, lowBound=0)
    grid_to_boiler = pulp.LpVariable.dicts("grid_to_boiler", idx, lowBound=0)
    grid_to_battery = pulp.LpVariable.dicts("grid_to_battery", idx, lowBound=0)

    battery_to_fixed_load = pulp.LpVariable.dicts("battery_to_fixed_load", idx, lowBound=0)
    battery_to_boiler = pulp.LpVariable.dicts("battery_to_boiler", idx, lowBound=0)
    battery_to_grid = pulp.LpVariable.dicts("battery_to_grid", idx, lowBound=0)

    soc = pulp.LpVariable.dicts("soc", range(n + 1), lowBound=0, upBound=capacity_kwh)

    boiler_phase_on = {
        (t, p): pulp.LpVariable(f"boiler_phase_{p}_on_{t}", cat=pulp.LpBinary)
        for t in idx
        for p in range(phase_count)
    }

    grid_import_direction = pulp.LpVariable.dicts(
        "grid_import_direction", idx, cat=pulp.LpBinary
    )
    battery_charge_direction = pulp.LpVariable.dicts(
        "battery_charge_direction", idx, cat=pulp.LpBinary
    )
    grid_charge_active = pulp.LpVariable.dicts(
        "grid_charge_active", idx, cat=pulp.LpBinary
    )

    ev_delivered = {}
    if ev_request is not None:
        for t in range(ev_request.window_start_idx, ev_request.window_end_idx + 1):
            ev_delivered[t] = pulp.LpVariable(
                f"ev_delivered_{t}", lowBound=0,
                upBound=ev_request.planning_power_kw * slot_hours,
            )
    ev_unserved_kwh = pulp.LpVariable("ev_unserved_kwh", lowBound=0)

    boiler_hard_unserved_kwh = pulp.LpVariable("boiler_hard_unserved_kwh", lowBound=0)

    # --- Ukotvení počátečního SoC (ARCH sekce 10.4) ---------------------
    prob += soc[0] == initial_soc_kwh

    # --- Energetická bilance + SoC + omezení pro každý slot -------------
    for t in idx:
        s = slots[t]
        pv_kwh = s.pv_kwh
        fixed_load_kwh = s.fixed_load_kwh
        ev_this_slot = ev_delivered.get(t)

        # PV bilance (ARCH sekce 10.3)
        prob += (
            pv_to_fixed_load[t] + pv_to_boiler[t] + pv_to_battery[t]
            + pv_to_grid[t] + pv_curtailed[t] == pv_kwh
        )

        # fixed_load bilance (+ EV, pokud je v okně - přičte se jako
        # dodatečná spotřeba kryta stejnými zdroji jako fixed_load)
        total_fixed = fixed_load_kwh + (ev_this_slot if ev_this_slot is not None else 0)
        prob += (
            pv_to_fixed_load[t] + grid_to_fixed_load[t] + battery_to_fixed_load[t]
            == total_fixed
        )

        # bojler bilance a vazba na binární fáze
        boiler_energy = pv_to_boiler[t] + grid_to_boiler[t] + battery_to_boiler[t]
        prob += boiler_energy == cfg.boiler.phase_power_kw * slot_hours * pulp.lpSum(
            boiler_phase_on[(t, p)] for p in range(phase_count)
        )

        grid_import_expr = grid_to_fixed_load[t] + grid_to_boiler[t] + grid_to_battery[t]
        grid_export_expr = pv_to_grid[t] + battery_to_grid[t]

        # zákaz současného importu a exportu
        prob += grid_import_expr <= max_grid_energy * grid_import_direction[t]
        prob += grid_export_expr <= max_grid_energy * (1 - grid_import_direction[t])

        # export zakázán pod prahem (ARCH sekce 5.1, "P < 20 => zakázáno")
        if not s.export_allowed:
            prob += pv_to_grid[t] == 0
            prob += battery_to_grid[t] == 0

        # SoC model v1 (ARCH sekce 10.4) - bez samostatné účinnosti
        charge_energy = pv_to_battery[t] + grid_to_battery[t]
        discharge_energy = battery_to_fixed_load[t] + battery_to_boiler[t] + battery_to_grid[t]
        prob += soc[t + 1] == soc[t] + charge_energy - discharge_energy

        # zákaz současného nabíjení a vybíjení
        prob += charge_energy <= max_charge_energy * battery_charge_direction[t]
        prob += discharge_energy <= max_discharge_energy * (1 - battery_charge_direction[t])

        # síťové nabíjení -> max 95 % na konci slotu; jinak max 100 %
        prob += grid_to_battery[t] <= max_charge_energy * grid_charge_active[t]
        prob += soc[t + 1] <= max_soc_pv_kwh - (
            max_soc_pv_kwh - max_soc_grid_kwh
        ) * grid_charge_active[t]
        prob += soc[t + 1] >= min_soc_kwh

        # efektivně neplacený import -> baterie se nesmí vybíjet (ARCH 10.5/11)
        if s.effective_import_nonpositive:
            prob += battery_to_fixed_load[t] == 0
            prob += battery_to_boiler[t] == 0
            prob += battery_to_grid[t] == 0

        # celkový výkon střídače <=10 kW (APROXIMACE, viz docstring modulu)
        prob += (
            grid_import_expr + grid_export_expr + charge_energy + discharge_energy
            <= max_grid_energy
        )

    # --- EV požadavek (level 1 slack, ARCH sekce 8.1/10.7) --------------
    if ev_request is not None:
        prob += (
            pulp.lpSum(ev_delivered.values()) + ev_unserved_kwh
            == ev_request.required_ac_kwh
        )
    else:
        prob += ev_unserved_kwh == 0

    # --- Pomocné agregované výrazy pro objective/extrakci ---------------
    grid_import_exprs = [
        grid_to_fixed_load[t] + grid_to_boiler[t] + grid_to_battery[t] for t in idx
    ]
    grid_export_exprs = [pv_to_grid[t] + battery_to_grid[t] for t in idx]
    boiler_energy_exprs = [
        pv_to_boiler[t] + grid_to_boiler[t] + battery_to_boiler[t] for t in idx
    ]
    battery_discharge_exprs = [
        battery_to_fixed_load[t] + battery_to_boiler[t] + battery_to_grid[t] for t in idx
    ]
    battery_charge_exprs = [pv_to_battery[t] + grid_to_battery[t] for t in idx]
    boiler_hard_energy = pulp.LpVariable.dicts("boiler_hard_energy", idx, lowBound=0)
    boiler_opportunistic_energy = pulp.LpVariable.dicts("boiler_opportunistic_energy", idx, lowBound=0)

    for t in idx:
        prob += (
            boiler_hard_energy[t] + boiler_opportunistic_energy[t]
            == boiler_energy_exprs[t]
        )

    # --- Bojler hard deadline (level 1 slack, ARCH sekce 8.2/10.7) ------
    if boiler_hard_request is not None:
        deadline_idx = min(boiler_hard_request.deadline_idx, n - 1)
        delivered_before_deadline = pulp.lpSum(
            boiler_hard_energy[t]
            for t in range(0, deadline_idx + 1)
        )
        prob += (
            delivered_before_deadline + boiler_hard_unserved_kwh
            == boiler_hard_request.required_kwh
        )
        for t in range(deadline_idx + 1, n):
            prob += boiler_hard_energy[t] == 0
    else:
        prob += boiler_hard_unserved_kwh == 0
        for t in idx:
            prob += boiler_hard_energy[t] == 0

    if boiler_opportunistic_daily_limits_kwh is not None:
        slots_by_date: dict[date, list[int]] = {}
        for t in idx:
            slots_by_date.setdefault(slots[t].slot_start.date(), []).append(t)
        for local_date, date_slots in slots_by_date.items():
            limit = max(0.0, float(boiler_opportunistic_daily_limits_kwh.get(local_date, 0.0)))
            prob += (
                pulp.lpSum(boiler_opportunistic_energy[t] for t in date_slots) <= limit,
                f"boiler_opportunistic_daily_limit_{local_date.isoformat()}",
            )
    else:
        if boiler_opportunistic_limit_kwh is None:
            boiler_opportunistic_limit_kwh = cfg.boiler.initial_full_heat_max_kwh
        prob += pulp.lpSum(boiler_opportunistic_energy[t] for t in idx) <= max(0.0, boiler_opportunistic_limit_kwh)

    # --- Terminální hodnota (ARCH sekce 6.4) ----------------------------
    terminal_soc_usable_kwh = soc[n] - min_soc_kwh

    economic_objective = (
        pulp.lpSum(
            grid_import_exprs[t] * slots[t].price_import_czk_kwh for t in idx
        )
        - pulp.lpSum(
            grid_export_exprs[t] * slots[t].price_export_czk_kwh for t in idx
        )
        - pulp.lpSum(boiler_energy_exprs[t] * gas_heat_czk_kwh for t in idx)
        + pulp.lpSum(battery_discharge_exprs[t] * battery_cycle_czk_kwh for t in idx)
        - terminal_soc_usable_kwh * terminal_value_czk_per_kwh
    )

    # ========================================================================
    # STAGE 1: minimalizovat kritické slacky (ARCH sekce 9/10.7 úroveň 1)
    # ========================================================================
    prob += ev_unserved_kwh + boiler_hard_unserved_kwh
    result1 = solver_adapter.solve(
        prob, cfg.solver.time_limit_seconds, cfg.solver.mip_gap
    )
    if not result1.is_optimal:
        return OptimizerResult(status=result1.status)

    stage1_slack = pulp.value(ev_unserved_kwh) + pulp.value(boiler_hard_unserved_kwh)

    # ========================================================================
    # STAGE 2: fixovat stage 1, minimalizovat ekonomiku (úroveň 2)
    # ========================================================================
    prob += (
        ev_unserved_kwh + boiler_hard_unserved_kwh <= stage1_slack + _EPS,
        "stage1_slack_fix",
    )
    prob.objective = economic_objective
    result2 = solver_adapter.solve(
        prob, cfg.solver.time_limit_seconds, cfg.solver.mip_gap
    )
    if not result2.is_optimal:
        return OptimizerResult(status=result2.status)

    stage2_objective = pulp.value(economic_objective)

    # ========================================================================
    # STAGE 3: fixovat stage 2 s tolerancí, minimalizovat bateriový
    # throughput (částečná úroveň 3 - viz docstring modulu)
    # ========================================================================
    tie_tolerance = cfg.solver.economic_tie_tolerance_czk
    prob += (
        economic_objective <= stage2_objective + tie_tolerance,
        "stage2_objective_fix",
    )
    throughput_objective = pulp.lpSum(battery_charge_exprs) + pulp.lpSum(
        battery_discharge_exprs
    )
    prob.objective = throughput_objective
    result3 = solver_adapter.solve(
        prob, cfg.solver.time_limit_seconds, cfg.solver.mip_gap
    )
    final_status = result3.status if result3.is_optimal else result2.status

    # --- Extrakce výsledku ------------------------------------------------
    slot_results = []
    for t in idx:
        s = slots[t]
        soc_start = pulp.value(soc[t])
        soc_end = pulp.value(soc[t + 1])
        g2b = pulp.value(grid_to_battery[t])
        b2g = pulp.value(battery_to_grid[t])
        b2fl = pulp.value(battery_to_fixed_load[t])
        b2boil = pulp.value(battery_to_boiler[t])
        g2fl = pulp.value(grid_to_fixed_load[t])
        g2boil = pulp.value(grid_to_boiler[t])

        action = _classify_battery_action(
            g2b, b2g, b2fl, b2boil, g2fl, g2boil, soc_start, min_soc_kwh
        )

        slot_results.append(
            SlotResult(
                slot_start=s.slot_start,
                pv_to_fixed_load_kwh=pulp.value(pv_to_fixed_load[t]),
                pv_to_boiler_kwh=pulp.value(pv_to_boiler[t]),
                pv_to_battery_kwh=pulp.value(pv_to_battery[t]),
                pv_to_grid_kwh=pulp.value(pv_to_grid[t]),
                pv_curtailed_kwh=pulp.value(pv_curtailed[t]),
                grid_to_fixed_load_kwh=g2fl,
                grid_to_boiler_kwh=g2boil,
                grid_to_battery_kwh=g2b,
                battery_to_fixed_load_kwh=b2fl,
                battery_to_boiler_kwh=b2boil,
                battery_to_grid_kwh=b2g,
                soc_start_kwh=soc_start,
                soc_end_kwh=soc_end,
                boiler_phase_on=tuple(
                    bool(round(pulp.value(boiler_phase_on[(t, p)])))
                    for p in range(phase_count)
                ),
                ev_delivered_kwh=(
                    pulp.value(ev_delivered[t]) if t in ev_delivered else 0.0
                ),
                grid_import_kwh=g2fl + g2boil + g2b,
                grid_export_kwh=pulp.value(pv_to_grid[t]) + b2g,
                battery_action=action,
                boiler_hard_kwh=pulp.value(boiler_hard_energy[t]),
                boiler_opportunistic_kwh=pulp.value(boiler_opportunistic_energy[t]),
            )
        )

    return OptimizerResult(
        status=final_status,
        slots=slot_results,
        ev_unserved_kwh=pulp.value(ev_unserved_kwh),
        boiler_hard_unserved_kwh=pulp.value(boiler_hard_unserved_kwh),
        economic_objective_czk=stage2_objective,
        terminal_soc_kwh=pulp.value(soc[n]),
    )
