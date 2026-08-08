"""Testy pro lib/optimizer.py - klíčové MILP scénáře.

Kryje syntetické scénáře z SERVER_IMPLEMENTATION_GUIDE_v10.md sekce 14
(acceptance scénáře 1, 3, 4 (delta 41.3223), 6 (import nonpositive), 7
(export 20 hranice), 8 (EV), 10 (bojler vs plyn), 12 (terminal value)).

Spouštět na serveru: cd planner_v10 && python3 tests/run_manual.py
(pytest NENÍ na serveru nainstalován).
"""
import copy
import os
import sys
import tomllib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import economics as ec  # noqa: E402
from lib import optimizer as opt  # noqa: E402
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


def _slot(
    cfg,
    offset: int = 0,
    price_import_spot: float = 50.0,
    price_export_spot: float = 50.0,
    export_allowed: bool = True,
    effective_import_nonpositive: bool = False,
    pv_kwh: float = 0.0,
    fixed_load_kwh: float = 0.0,
) -> opt.SlotInput:
    return opt.SlotInput(
        slot_start=_BASE_START + timedelta(minutes=cfg.system.planning_step_minutes * offset),
        price_import_czk_kwh=ec.import_cost_czk_per_kwh(price_import_spot, cfg),
        price_export_czk_kwh=ec.export_revenue_czk_per_kwh(price_export_spot, cfg),
        export_allowed=export_allowed,
        effective_import_nonpositive=effective_import_nonpositive,
        pv_kwh=pv_kwh,
        fixed_load_kwh=fixed_load_kwh,
    )


def _floor_kwh(cfg) -> float:
    return cfg.battery.min_soc_pct / 100.0 * cfg.battery.capacity_kwh


# ============================================================================

def test_basic_balance_grid_covers_load():
    cfg = _cfg()
    slots = [_slot(cfg, fixed_load_kwh=1.0, pv_kwh=0.0)]
    result = opt.optimize(slots, cfg, initial_soc_kwh=_floor_kwh(cfg))
    assert result.status == "optimal"
    s0 = result.slots[0]
    assert abs(s0.grid_to_fixed_load_kwh - 1.0) < 1e-4
    assert abs(s0.grid_import_kwh - 1.0) < 1e-4
    assert abs(s0.battery_to_fixed_load_kwh) < 1e-4
    assert abs(s0.soc_end_kwh - s0.soc_start_kwh) < 1e-4


def test_grid_charge_self_use_profitable_scenario():
    """Profitabilní síť->baterie->vlastní spotřeba (delta 41.3223, ARCH 5.3).

    POZOR: `economic_tie_tolerance_czk` (úroveň 3, throughput tie-break) by
    při malé marži dovolil stage 3 mírně obětovat ekonomiku výměnou za nižší
    throughput (ARCH sekce 10.7 úroveň 3) - proto zde explicitně nastavujeme
    tie_tolerance=0, aby test izolovaně ověřil jen stage 2 (ekonomické)
    rozhodnutí, ne interakci se stage 3."""
    cfg = _cfg({"solver": {
        "time_limit_seconds": 30, "mip_gap": 0.001, "economic_tie_tolerance_czk": 0.0,
    }})
    ref = ec.derived_reference_values(cfg)
    delta = ref["self_use_spot_delta_eur_mwh"]
    buy = 50.0
    future = buy + delta + 5.0  # jistě nad hranicí ziskovosti

    slots = [
        _slot(cfg, offset=0, price_import_spot=buy, price_export_spot=buy, fixed_load_kwh=0.0),
        _slot(cfg, offset=1, price_import_spot=future, price_export_spot=future, fixed_load_kwh=1.0),
    ]
    result = opt.optimize(slots, cfg, initial_soc_kwh=_floor_kwh(cfg))
    assert result.status == "optimal"
    s0, s1 = result.slots
    # nabito ze site v levnem slotu, spotreba slotu 1 kryta z baterie
    assert s0.grid_to_battery_kwh > 0.9
    assert s1.battery_to_fixed_load_kwh > 0.9
    assert s1.grid_to_fixed_load_kwh < 0.1



def test_grid_charge_not_profitable_below_threshold():
    """Pod hranicí ziskovosti (delta - 5) se NEMÁ vyplatit nabíjet ze sítě."""
    cfg = _cfg()
    ref = ec.derived_reference_values(cfg)
    delta = ref["self_use_spot_delta_eur_mwh"]
    buy = 50.0
    future = buy + delta - 5.0  # pod hranici ziskovosti

    slots = [
        _slot(cfg, offset=0, price_import_spot=buy, price_export_spot=buy, fixed_load_kwh=0.0),
        _slot(cfg, offset=1, price_import_spot=future, price_export_spot=future, fixed_load_kwh=1.0),
    ]
    result = opt.optimize(slots, cfg, initial_soc_kwh=_floor_kwh(cfg))
    assert result.status == "optimal"
    s0, s1 = result.slots
    assert s0.grid_to_battery_kwh < 0.1
    assert s1.grid_to_fixed_load_kwh > 0.9


def test_export_disabled_below_threshold():
    cfg = _cfg()
    slots = [
        _slot(cfg, price_import_spot=10.0, price_export_spot=10.0, export_allowed=False,
              pv_kwh=2.0, fixed_load_kwh=0.0)
    ]
    result = opt.optimize(slots, cfg, initial_soc_kwh=_floor_kwh(cfg))
    assert result.status == "optimal"
    s0 = result.slots[0]
    assert abs(s0.pv_to_grid_kwh) < 1e-4
    assert abs(s0.battery_to_grid_kwh) < 1e-4
    # PV prebytek musi jit jinam (baterie/bojler/curtail), energie se nesmi
    # ztratit bez stopy (pv_to_boiler je take validni sink, ARCH sekce 10.3)
    assert abs(
        (s0.pv_to_fixed_load_kwh + s0.pv_to_boiler_kwh + s0.pv_to_battery_kwh
         + s0.pv_curtailed_kwh) - 2.0
    ) < 1e-4



def test_soc_floor_respected_battery_cannot_discharge_below_floor():
    """Load 2.0 kWh/slot je v rámci fyzikálních limitů (inverter_total_kw=10,
    tedy max 2.5 kWh/15min slot dle config.toml) - viz optimizer.py docstring
    "celkový výkon střídače <=10 kW" (APROXIMACE, ARCH sekce 10.5)."""
    cfg = _cfg()
    slots = [_slot(cfg, fixed_load_kwh=2.0, pv_kwh=0.0)]
    result = opt.optimize(slots, cfg, initial_soc_kwh=_floor_kwh(cfg))
    assert result.status == "optimal"
    s0 = result.slots[0]
    assert abs(s0.battery_to_fixed_load_kwh) < 1e-4
    assert abs(s0.grid_to_fixed_load_kwh - 2.0) < 1e-4
    assert s0.soc_end_kwh >= _floor_kwh(cfg) - 1e-4



def test_effective_import_nonpositive_blocks_discharge():
    cfg = _cfg()
    half_soc = cfg.battery.capacity_kwh * 0.5
    slots = [
        _slot(cfg, fixed_load_kwh=1.0, pv_kwh=0.0, effective_import_nonpositive=True)
    ]
    result = opt.optimize(slots, cfg, initial_soc_kwh=half_soc)
    assert result.status == "optimal"
    s0 = result.slots[0]
    assert abs(s0.battery_to_fixed_load_kwh) < 1e-4
    assert abs(s0.grid_to_fixed_load_kwh - 1.0) < 1e-4


def test_grid_charge_capped_at_95_percent():
    """Sitove nabijeni nesmi prekrocit max_soc_grid_pct (95 %, ARCH 10.4).

    POZOR: `inverter_total_kw`/`max_charge_kw`/`max_discharge_kw` musí být
    navýšené i v testovacím grid override, jinak fyzikální limit slotu
    (viz optimizer.py "celkový výkon střídače <=10 kW" ARCH 10.5) sám o
    sobě zamezí dostatečnému nabití pro test stropu 95 %."""
    cfg = _cfg({
        "battery": {
            "capacity_kwh": 2.0, "min_soc_pct": 20.0, "max_soc_pv_pct": 100.0,
            "max_soc_grid_pct": 95.0, "max_charge_kw": 50.0, "max_discharge_kw": 50.0,
            "terminal_value_lookahead_hours": 12, "hold_power_pct": 1,
        },
        "grid": {
            "main_breaker_a": 32.0, "soft_phase_limit_a": 30.0,
            "phase_nominal_voltage_v": 230.0, "inverter_total_kw": 100.0,
        },
    })
    floor = _floor_kwh(cfg)
    ref = ec.derived_reference_values(cfg)
    delta = ref["self_use_spot_delta_eur_mwh"]
    buy = 50.0
    future = buy + delta + 20.0  # silne ziskove -> chce se nabit co nejvic

    slots = [
        _slot(cfg, offset=0, price_import_spot=buy, price_export_spot=buy, fixed_load_kwh=0.0),
        _slot(cfg, offset=1, price_import_spot=future, price_export_spot=future, fixed_load_kwh=1.5),
    ]
    result = opt.optimize(slots, cfg, initial_soc_kwh=floor)
    assert result.status == "optimal"
    s0 = result.slots[0]
    max_soc_grid_kwh = cfg.battery.max_soc_grid_pct / 100.0 * cfg.battery.capacity_kwh
    assert s0.soc_end_kwh <= max_soc_grid_kwh + 1e-4
    # a skutecne se nabijelo blizko stropu (ne skoro nic)
    assert s0.soc_end_kwh > max_soc_grid_kwh - 0.3



def test_ev_request_served_within_window():
    cfg = _cfg()
    slots = [_slot(cfg, offset=i, fixed_load_kwh=0.0) for i in range(3)]
    ev_req = opt.EvRequest(
        window_start_idx=1, window_end_idx=1,
        required_ac_kwh=0.5, planning_power_kw=cfg.ev.planning_power_kw,
    )
    result = opt.optimize(slots, cfg, initial_soc_kwh=_floor_kwh(cfg), ev_request=ev_req)
    assert result.status == "optimal"
    assert abs(result.ev_unserved_kwh) < 1e-4
    assert abs(result.slots[1].ev_delivered_kwh - 0.5) < 1e-4
    assert abs(result.slots[0].ev_delivered_kwh) < 1e-9
    assert abs(result.slots[2].ev_delivered_kwh) < 1e-9


def test_ev_request_infeasible_produces_slack():
    cfg = _cfg()
    slots = [_slot(cfg, offset=0, fixed_load_kwh=0.0)]
    max_per_slot = cfg.ev.planning_power_kw * cfg.system.planning_step_minutes / 60.0
    required = 5.0  # zdaleka vic, nez lze v jednom slotu dodat
    ev_req = opt.EvRequest(
        window_start_idx=0, window_end_idx=0,
        required_ac_kwh=required, planning_power_kw=cfg.ev.planning_power_kw,
    )
    result = opt.optimize(slots, cfg, initial_soc_kwh=_floor_kwh(cfg), ev_request=ev_req)
    assert result.status == "optimal"
    assert abs(result.slots[0].ev_delivered_kwh - max_per_slot) < 1e-3
    assert abs(result.ev_unserved_kwh - (required - max_per_slot)) < 1e-3


def test_boiler_hard_deadline_served_before_deadline():
    cfg = _cfg()
    slots = [_slot(cfg, offset=i, fixed_load_kwh=0.0) for i in range(3)]
    boiler_req = opt.BoilerHardRequest(deadline_idx=1, required_kwh=1.0)
    result = opt.optimize(
        slots, cfg, initial_soc_kwh=_floor_kwh(cfg), boiler_hard_request=boiler_req
    )
    assert result.status == "optimal"
    assert abs(result.boiler_hard_unserved_kwh) < 1e-3
    delivered_before_deadline = sum(
        r.pv_to_boiler_kwh + r.grid_to_boiler_kwh + r.battery_to_boiler_kwh
        for r in result.slots[:2]
    )
    assert delivered_before_deadline >= 1.0 - 1e-3


def test_boiler_hard_deadline_infeasible_produces_slack():
    cfg = _cfg()
    slots = [_slot(cfg, offset=0, fixed_load_kwh=0.0)]
    max_boiler_energy = (
        cfg.boiler.phase_count * cfg.boiler.phase_power_kw
        * cfg.system.planning_step_minutes / 60.0
    )
    required = 10.0  # zdaleka vic nez fyzicky lze dodat v 1 slotu
    boiler_req = opt.BoilerHardRequest(deadline_idx=0, required_kwh=required)
    result = opt.optimize(
        slots, cfg, initial_soc_kwh=_floor_kwh(cfg), boiler_hard_request=boiler_req
    )
    assert result.status == "optimal"
    assert result.boiler_hard_unserved_kwh > required - max_boiler_energy - 1e-3


def test_boiler_opportunistic_heating_is_capped_separately_from_hard_request():
    cfg = _cfg({"solver": {"economic_tie_tolerance_czk": 0.0}})
    slots = [
        _slot(cfg, offset=i, price_import_spot=-100.0, price_export_spot=-100.0, fixed_load_kwh=0.0)
        for i in range(4)
    ]

    result = opt.optimize(
        slots,
        cfg,
        initial_soc_kwh=_floor_kwh(cfg),
        boiler_opportunistic_limit_kwh=1.0,
    )

    assert result.status == "optimal"
    opportunistic = sum(s.boiler_opportunistic_kwh for s in result.slots)
    hard = sum(s.boiler_hard_kwh for s in result.slots)
    assert hard < 1e-6
    assert opportunistic <= 1.0 + 1e-4
    assert opportunistic > 0.9


def test_boiler_opportunistic_daily_limits_are_independent_across_midnight():
    cfg = _cfg({"solver": {"economic_tie_tolerance_czk": 0.0}})
    start = datetime(2026, 8, 2, 23, 30)
    slots = []
    for offset in range(4):
        item = _slot(cfg, offset=offset, price_import_spot=-100.0, price_export_spot=-100.0)
        object.__setattr__(item, "slot_start", start + timedelta(minutes=15 * offset))
        slots.append(item)
    result = opt.optimize(
        slots, cfg, initial_soc_kwh=_floor_kwh(cfg),
        boiler_opportunistic_daily_limits_kwh={start.date(): 1.0, (start + timedelta(days=1)).date(): 2.0},
    )
    first = sum(slot.boiler_opportunistic_kwh for slot in result.slots if slot.slot_start.date() == start.date())
    second = sum(slot.boiler_opportunistic_kwh for slot in result.slots if slot.slot_start.date() != start.date())
    assert result.status == "optimal"
    assert 0.9 < first <= 1.0001
    assert 1.9 < second <= 2.0001


def test_terminal_value_prevents_needless_discharge():
    """Vysoka terminalni hodnota MA zabranit zbytecnemu vybiti/exportu na
    konci horizontu (ARCH sekce 6.4, SERVER_IMPLEMENTATION_GUIDE_v10.md 11.4).

    POZOR: import a export cena v testovacím slotu musí zůstat NEZÁVISLÉ -
    pokud by import_cost byl nízký (levný), terminální hodnota vyšší než
    export_revenue-cycle_cost by paradoxně vedla k DALŠÍMU dobíjení ze sítě
    jen pro terminální bonus (viz ARCH 6.4 "terminální hodnota nesmí být
    optimističtější než realistický import" - přesně tuto past demonstruje
    debug scénář v HANDOFF_v10.md). Proto zde import_spot držíme VYSOKO
    (drahý), aby dobíjení ze sítě nebylo nikdy přitažlivé, a testujeme jen
    to, že baterie nebude ZBYTEČNĚ vybita/exportována."""
    cfg = _cfg()
    half_soc = cfg.battery.capacity_kwh * 0.5
    export_spot = 250.0  # vysoká cena, export by jinak byl výhodný
    import_spot = 250.0  # stejně drahý import - dobíjení ze sítě se NIKDY nevyplatí
    slots = [_slot(cfg, price_import_spot=import_spot, price_export_spot=export_spot,
                    export_allowed=True, pv_kwh=0.0, fixed_load_kwh=0.0)]

    # bez terminalni hodnoty - vyplati se exportovat (cena vysoko nad cyklovym nakladem)
    result_no_terminal = opt.optimize(
        slots, cfg, initial_soc_kwh=half_soc, terminal_value_czk_per_kwh=0.0
    )
    assert result_no_terminal.status == "optimal"
    assert result_no_terminal.slots[0].battery_to_grid_kwh > 0.1

    # Terminalni hodnota MEZI (export_revenue - cycle_cost) a export_revenue
    # samotnym staci na zastaveni exportu (drzet energii je vyhodnejsi nez
    # prodat a ztratit cyklovy naklad). Zaroven je hluboko POD import_cost
    # stejneho (draheho) slotu, takze NEVZNIKA motivace dobit ze site navic.
    export_revenue_czk_kwh = ec.export_revenue_czk_per_kwh(export_spot, cfg)
    cycle_cost_czk_kwh = ec.battery_cycle_cost_czk_per_kwh(cfg)
    terminal_value = export_revenue_czk_kwh - cycle_cost_czk_kwh + 1.0
    result_with_terminal = opt.optimize(
        slots, cfg, initial_soc_kwh=half_soc, terminal_value_czk_per_kwh=terminal_value
    )
    assert result_with_terminal.status == "optimal"
    assert result_with_terminal.slots[0].battery_to_grid_kwh < 1e-3
    assert result_with_terminal.slots[0].grid_to_battery_kwh < 1e-3
    assert abs(result_with_terminal.slots[0].soc_end_kwh - half_soc) < 1e-3




def test_no_simultaneous_charge_and_discharge_or_import_export():
    """Strukturalni sanity check - i v protichudnem scenari (levny import a
    soucasne vyhodny export) model nikdy nedela obe smery najednou."""
    cfg = _cfg()
    slots = [
        _slot(cfg, price_import_spot=-50.0, price_export_spot=300.0,
              export_allowed=True, pv_kwh=1.0, fixed_load_kwh=1.0)
    ]
    result = opt.optimize(slots, cfg, initial_soc_kwh=_floor_kwh(cfg) + 1.0)
    assert result.status == "optimal"
    s0 = result.slots[0]
    charge = s0.pv_to_battery_kwh + s0.grid_to_battery_kwh
    discharge = s0.battery_to_fixed_load_kwh + s0.battery_to_boiler_kwh + s0.battery_to_grid_kwh
    assert charge < 1e-6 or discharge < 1e-6
    grid_import = s0.grid_import_kwh
    grid_export = s0.grid_export_kwh
    assert grid_import < 1e-6 or grid_export < 1e-6


def test_empty_slots_returns_infeasible_status():
    cfg = _cfg()
    result = opt.optimize([], cfg, initial_soc_kwh=_floor_kwh(cfg))
    assert result.status == "infeasible"
    assert result.slots == []


def test_stage3_nonoptimal_restores_certified_stage2_solution():
    cfg = _cfg()
    slots = [_slot(cfg, fixed_load_kwh=0.5, pv_kwh=0.0)]
    original_solve = opt.solver_adapter.solve
    calls = 0
    stage2_values = {}

    def fake_solve(problem, time_limit_seconds, mip_gap):
        nonlocal calls, stage2_values
        calls += 1
        result = original_solve(problem, time_limit_seconds, mip_gap)
        if calls == 2:
            stage2_values = {variable.name: variable.varValue for variable in problem.variables()}
        elif calls == 3:
            for variable in problem.variables():
                if variable.name.startswith("soc_"):
                    variable.varValue = 0.0
            return opt.solver_adapter.SolveResult(
                status=opt.solver_adapter.STATUS_FEASIBLE,
                objective_value=0.0,
                solver_name="test",
            )
        return result

    opt.solver_adapter.solve = fake_solve
    try:
        result = opt.optimize(slots, cfg, initial_soc_kwh=_floor_kwh(cfg))
    finally:
        opt.solver_adapter.solve = original_solve

    assert calls == 3
    assert result.status == opt.solver_adapter.STATUS_OPTIMAL
    assert abs(result.slots[0].soc_start_kwh - stage2_values["soc_0"]) < 1e-6
    assert abs(result.slots[0].soc_end_kwh - stage2_values["soc_1"]) < 1e-6


def test_stage3_uses_dedicated_short_time_limit():
    cfg = _cfg({"solver": {"time_limit_seconds": 60, "tie_break_time_limit_seconds": 5}})
    slots = [_slot(cfg, fixed_load_kwh=0.5)]
    original_solve = opt.solver_adapter.solve
    limits = []

    def capture_solve(problem, time_limit_seconds, mip_gap):
        limits.append(time_limit_seconds)
        return original_solve(problem, time_limit_seconds, mip_gap)

    opt.solver_adapter.solve = capture_solve
    try:
        result = opt.optimize(slots, cfg, initial_soc_kwh=_floor_kwh(cfg))
    finally:
        opt.solver_adapter.solve = original_solve

    assert result.status == "optimal"
    assert limits == [60, 60, 5]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
