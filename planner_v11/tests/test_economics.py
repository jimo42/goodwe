"""Golden testy pro lib/economics.py - ověření proti CONSISTENCY_CHECK.json.

Spouštět na serveru: cd planner_v10 && python3 -m pytest tests/test_economics.py -v
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import economics as ec
from lib.config import Config, load_config


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.toml")


def _cfg() -> Config:
    return load_config(CONFIG_PATH)


def approx(a, b, tol=1e-6):
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


def test_import_cost_basic():
    cfg = _cfg()
    # P = 0 => IMPORT_COST = vat * fixed_pre_vat = 1.21 * 2099.07
    val = ec.import_cost_czk_per_mwh(0.0, cfg)
    assert approx(val, 1.21 * 2099.07), val


def test_export_revenue_basic():
    cfg = _cfg()
    val = ec.export_revenue_czk_per_mwh(100.0, cfg)
    assert approx(val, 100.0 * 24.60 - 390.00), val


def test_battery_cycle_cost():
    cfg = _cfg()
    assert approx(ec.battery_cycle_cost_czk_per_mwh(cfg), 50.0 * 24.60)
    assert approx(ec.battery_cycle_cost_czk_per_kwh(cfg), 50.0 * 24.60 / 1000.0)


def test_gas_heat_value():
    cfg = _cfg()
    assert approx(ec.gas_heat_value_czk_per_mwh(cfg), 100.0 * 24.60)
    assert approx(ec.gas_heat_value_czk_per_kwh(cfg), 100.0 * 24.60 / 1000.0)


def test_export_allowed_threshold():
    cfg = _cfg()
    assert ec.is_export_allowed(20.0, cfg) is True
    assert ec.is_export_allowed(19.99, cfg) is False
    assert ec.is_export_allowed(100.0, cfg) is True


def test_effective_import_nonpositive_threshold():
    cfg = _cfg()
    ref = ec.derived_reference_values(cfg)
    boundary = ref["import_zero_spot_eur_mwh"]
    assert approx(boundary, -85.3280487804878, tol=1e-4)
    assert ec.is_effective_import_nonpositive(boundary, cfg) is True
    assert ec.is_effective_import_nonpositive(boundary - 1.0, cfg) is True
    assert ec.is_effective_import_nonpositive(boundary + 1.0, cfg) is False


def test_derived_reference_values_match_consistency_check():
    cfg = _cfg()
    ref = ec.derived_reference_values(cfg)
    expected = {
        "fixed_import_eur_mwh": 85.3280487804878,
        "export_fee_eur_mwh": 15.8536585365854,
        "import_zero_spot_eur_mwh": -85.3280487804878,
        "self_use_spot_delta_eur_mwh": 41.3223140495868,
        "grid_export_formula_constant": 169.100597560976,
        "direct_grid_boiler_break_even_spot_eur_mwh": -2.68342068131425,
        "direct_pv_boiler_vs_export_break_even_spot_eur_mwh": 115.853658536585,
    }
    for key, exp_val in expected.items():
        got = ref[key]
        assert approx(got, exp_val, tol=1e-4), f"{key}: got {got}, expected {exp_val}"


def test_grid_charge_self_use_profitable_boundary():
    cfg = _cfg()
    ref = ec.derived_reference_values(cfg)
    delta = ref["self_use_spot_delta_eur_mwh"]
    buy = 50.0
    # future = buy + delta => IMPORT_COST(future) - IMPORT_COST(buy) == battery_cycle_cost (boundary)
    future_boundary = buy + delta
    assert ec.grid_charge_self_use_profitable(buy, future_boundary, cfg) is True
    assert ec.grid_charge_self_use_profitable(buy, future_boundary - 1.0, cfg) is False
    assert ec.grid_charge_self_use_profitable(buy, future_boundary + 1.0, cfg) is True


def test_grid_charge_export_profitable_boundary():
    cfg = _cfg()
    ref = ec.derived_reference_values(cfg)
    const = ref["grid_export_formula_constant"]
    vat = cfg.economics.purchase_vat_multiplier
    buy = 50.0
    sell_boundary = vat * buy + const
    assert ec.grid_charge_export_profitable(buy, sell_boundary, cfg) is True
    assert ec.grid_charge_export_profitable(buy, sell_boundary - 1.0, cfg) is False
    assert ec.grid_charge_export_profitable(buy, sell_boundary + 1.0, cfg) is True


def test_no_duplicate_deprecated_25eur_surcharge():
    """Regresní test: battery_cycle_cost NESMÍ obsahovat dodatečnou
    25 EUR/MWh přirážku navíc k battery_cycle_cost_eur_per_mwh (deprecated
    logika zmíněná v CONTROL_LOGIC_SPEC_v10.yaml)."""
    cfg = _cfg()
    val = ec.battery_cycle_cost_czk_per_mwh(cfg)
    naive_with_surcharge = (cfg.economics.battery_cycle_cost_eur_per_mwh + 25.0) * cfg.economics.czk_per_eur
    assert not approx(val, naive_with_surcharge)
    assert approx(val, cfg.economics.battery_cycle_cost_eur_per_mwh * cfg.economics.czk_per_eur)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
