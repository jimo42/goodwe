"""
Jediná knihovna ekonomických vzorců pro planner v10.

Autoritativní zdroj vzorců a konstant:
  - ARCHITECTURE_DESIGN_v10_FINAL.md sekce 5 (Ekonomický model)
  - CONTROL_LOGIC_SPEC_v10.yaml sekce `economics`
  - SERVER_IMPLEMENTATION_GUIDE_v10.md sekce 10 (Ekonomická implementace)

MUST (závazné, viz dokumenty výše):
  - Import: IMPORT_COST_CZK_MWH(P) = purchase_vat_multiplier * (P * czk_per_eur
    + purchase_fixed_czk_per_mwh_pre_vat)
  - Export: EXPORT_REVENUE_CZK_MWH(P) = P * czk_per_eur - export_service_czk_per_mwh
  - Bateriový all-in cyklový náklad (battery_cycle_cost_eur_per_mwh, výchozí 50
    EUR/MWh) se účtuje PRÁVĚ JEDNOU na AC energii vydanou z baterie (do domu,
    bojleru i sítě) - žádná další samostatná účinnost, žádná extra 25 EUR/MWh
    bezpečnostní přirážka (viz "deprecated_thresholds"/"deprecated_logic" v
    CONTROL_LOGIC_SPEC_v10.yaml).
  - Hodnota tepla z plynu (gas_heat_value_eur_per_mwh, výchozí 100 EUR/MWh
    tepla) se používá k ocenění elektrického ohřevu bojleru náhradou plynu.
  - Export je zakázán, pokud je spotová cena striktně pod
    export_disable_spot_eur_mwh (výchozí 20 EUR/MWh) - "P < 20 => export MUST
    být zakázán", tedy povoleno je P >= práh.
  - Efektivní import je nekladný, pokud IMPORT_COST_CZK_MWH(P) <= 0 -
    NEPOUŽÍVAT zaokrouhlenou konstantu -85 jako druhý zdroj pravdy, jen jako
    orientační referenci v testech.

Všechny komponenty (optimizer, executor, analysis, testy) MUSÍ importovat
vzorce odtud - zákaz duplikace konstant/vzorců jinde v kódu.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.config import Config


# ============================================================================
# Základní cenové vzorce (CZK/MWh vstup i výstup, spot v EUR/MWh)
# ============================================================================

def import_cost_czk_per_mwh(spot_eur_mwh: float, cfg: "Config") -> float:
    """Cena nákupu (importu) ze sítě v CZK/MWh pro danou spotovou cenu.

    IMPORT_COST_CZK_MWH(P) = vat_multiplier * (P * czk_per_eur + fixed_pre_vat)
    """
    e = cfg.economics
    return e.purchase_vat_multiplier * (
        spot_eur_mwh * e.czk_per_eur + e.purchase_fixed_czk_per_mwh_pre_vat
    )


def export_revenue_czk_per_mwh(spot_eur_mwh: float, cfg: "Config") -> float:
    """Výnos z exportu (prodeje) do sítě v CZK/MWh pro danou spotovou cenu.

    EXPORT_REVENUE_CZK_MWH(P) = P * czk_per_eur - export_service_czk_per_mwh
    """
    e = cfg.economics
    return spot_eur_mwh * e.czk_per_eur - e.export_service_czk_per_mwh


def import_cost_czk_per_kwh(spot_eur_mwh: float, cfg: "Config") -> float:
    """Cena importu v CZK/kWh - pohodlný převod pro optimizer, který počítá
    v kWh za 15min slot (viz lib/optimizer.py). Stejný vzorec, jen /1000."""
    return import_cost_czk_per_mwh(spot_eur_mwh, cfg) / 1000.0


def export_revenue_czk_per_kwh(spot_eur_mwh: float, cfg: "Config") -> float:
    """Výnos z exportu v CZK/kWh - pohodlný převod pro optimizer (viz výše)."""
    return export_revenue_czk_per_mwh(spot_eur_mwh, cfg) / 1000.0



# ============================================================================
# Bateriový all-in cyklový náklad a hodnota tepla z plynu
# ============================================================================

def battery_cycle_cost_czk_per_mwh(cfg: "Config") -> float:
    """All-in ekonomická cena 1 MWh AC energie vydané z baterie, v CZK/MWh."""
    e = cfg.economics
    return e.battery_cycle_cost_eur_per_mwh * e.czk_per_eur


def battery_cycle_cost_czk_per_kwh(cfg: "Config") -> float:
    """All-in ekonomická cena 1 kWh AC energie vydané z baterie, v CZK/kWh.

    Při výchozích hodnotách (50 EUR/MWh, 24.60 CZK/EUR) = 1.23 CZK/kWh.
    """
    return battery_cycle_cost_czk_per_mwh(cfg) / 1000.0


def gas_heat_value_czk_per_mwh(cfg: "Config") -> float:
    """Hodnota 1 MWh tepla nahrazeného elektrickým ohřevem, v CZK/MWh."""
    return cfg.economics.gas_heat_value_eur_per_mwh * cfg.economics.czk_per_eur


def gas_heat_value_czk_per_kwh(cfg: "Config") -> float:
    """Hodnota 1 kWh tepla nahrazeného elektrickým ohřevem, v CZK/kWh.

    Při výchozích hodnotách (100 EUR/MWh, 24.60 CZK/EUR) = 2.46 CZK/kWh.
    """
    return gas_heat_value_czk_per_mwh(cfg) / 1000.0


# ============================================================================
# Rozhodovací predikáty
# ============================================================================

def is_export_allowed(spot_eur_mwh: float, cfg: "Config") -> bool:
    """True, pokud je export do sítě při dané spotové ceně povolen.

    "P < 20 EUR/MWh => export MUST být zakázán" - tedy povoleno je P >= práh
    (export_disable_spot_eur_mwh, výchozí 20.00).
    """
    return spot_eur_mwh >= cfg.economics.export_disable_spot_eur_mwh


def is_effective_import_nonpositive(spot_eur_mwh: float, cfg: "Config") -> bool:
    """True, pokud je efektivní cena importu při dané spotové ceně <= 0 CZK/MWh.

    Toto je podmínka pro "velmi levný režim" (very_cheap_mode) - baterie se
    nesmí vybíjet a nabíjí se až do 95 %. Použít VŽDY tuto funkci, ne
    zaokrouhlenou orientační konstantu ~-85.33 EUR/MWh.
    """
    return import_cost_czk_per_mwh(spot_eur_mwh, cfg) <= 0.0


def grid_charge_self_use_profitable(
    buy_spot_eur_mwh: float, future_spot_eur_mwh: float, cfg: "Config"
) -> bool:
    """True, pokud je ekonomické nabít baterii ze sítě při `buy_spot` pro
    pozdější nahrazení importu při `future_spot` (síť -> baterie -> vlastní
    spotřeba).

    Podmínka: IMPORT_COST(future) >= IMPORT_COST(buy) + battery_cycle_cost
    (vše v CZK/MWh).
    """
    return import_cost_czk_per_mwh(future_spot_eur_mwh, cfg) >= (
        import_cost_czk_per_mwh(buy_spot_eur_mwh, cfg)
        + battery_cycle_cost_czk_per_mwh(cfg)
    )


def grid_charge_export_profitable(
    buy_spot_eur_mwh: float, sell_spot_eur_mwh: float, cfg: "Config"
) -> bool:
    """True, pokud je ekonomické nabít baterii ze sítě při `buy_spot` pro
    pozdější prodej (export) při `sell_spot` (síť -> baterie -> export).

    Podmínka: EXPORT_REVENUE(sell) >= IMPORT_COST(buy) + battery_cycle_cost
    (vše v CZK/MWh). Export musí být navíc samostatně povolen
    (`is_export_allowed`) - tato funkce sama o sobě exportní zákaz nekontroluje.
    """
    return export_revenue_czk_per_mwh(sell_spot_eur_mwh, cfg) >= (
        import_cost_czk_per_mwh(buy_spot_eur_mwh, cfg)
        + battery_cycle_cost_czk_per_mwh(cfg)
    )


# ============================================================================
# Odvozené referenční hodnoty (jen pro testy/diagnostiku - NEJSOU druhým
# zdrojem pravdy pro rozhodovací logiku, ta vždy počítá přes funkce výše).
#
# Ověřeno proti input_from_other_ai/CONSISTENCY_CHECK.json['derived'] při
# výchozích hodnotách z config.toml.example (24.60 / 2099.07 / 1.21 / 390.00
# / 50.00 / 100.00):
#   fixed_import_eur_mwh                       = 85.3280487804878
#   export_fee_eur_mwh                         = 15.8536585365854
#   import_zero_spot_eur_mwh                   = -85.3280487804878
#   self_use_spot_delta_eur_mwh                = 41.3223140495868
#   grid_export_formula_constant               = 169.100597560976
#   direct_grid_boiler_break_even_spot_eur_mwh = -2.68342068131425
#   direct_pv_boiler_vs_export_break_even_spot_eur_mwh = 115.853658536585
# ============================================================================

def derived_reference_values(cfg: "Config") -> dict:
    """Vrátí odvozené ekonomické hraniční hodnoty (EUR/MWh) pro diagnostiku
    a golden testy - viz ARCHITECTURE_DESIGN_v10_FINAL.md sekce 5.3/5.4 a
    CONSISTENCY_CHECK.json['derived'].

    Tyto hodnoty se NESMÍ používat jako alternativní rozhodovací konstanty v
    produkčním kódu - slouží výhradně k ověření, že vzorce výše počítají
    správně (assert proti nezávisle odvozeným číslům z architektury).
    """
    e = cfg.economics
    czk = e.czk_per_eur
    fixed_pre_vat = e.purchase_fixed_czk_per_mwh_pre_vat
    vat = e.purchase_vat_multiplier
    export_fee = e.export_service_czk_per_mwh
    battery_cycle_eur = e.battery_cycle_cost_eur_per_mwh
    gas_eur = e.gas_heat_value_eur_per_mwh

    # IMPORT_COST_EUR_MWH(P) = vat * (P + fixed_import_eur_mwh)  [ARCH sekce 5.1]
    fixed_import_eur_mwh = fixed_pre_vat / czk
    # EXPORT_REVENUE_EUR_MWH(P) = P - export_fee_eur_mwh
    export_fee_eur_mwh = export_fee / czk
    # IMPORT_COST_CZK_MWH(P) <= 0  <=>  P <= -fixed_import_eur_mwh
    import_zero_spot_eur_mwh = -fixed_import_eur_mwh
    # H - B >= battery_cycle_eur / vat  (síť->baterie->pozdější vlastní spotřeba)
    self_use_spot_delta_eur_mwh = battery_cycle_eur / vat
    # EXPORT_REVENUE_EUR_MWH(S) >= IMPORT_COST_EUR_MWH(B) + battery_cycle_eur
    # S - export_fee_eur_mwh >= vat*(B + fixed_import_eur_mwh) + battery_cycle_eur
    # S >= vat*B + [vat*fixed_import_eur_mwh + battery_cycle_eur + export_fee_eur_mwh]
    grid_export_formula_constant = (
        vat * fixed_import_eur_mwh + battery_cycle_eur + export_fee_eur_mwh
    )
    # gas break-even (přímý síťový ohřev vs. plyn):
    # IMPORT_COST_EUR_MWH(P) < gas_eur  =>  P < gas_eur/vat - fixed_import_eur_mwh
    direct_grid_boiler_break_even_spot_eur_mwh = gas_eur / vat - fixed_import_eur_mwh
    # gas break-even (přímé PV do bojleru vs. export):
    # EXPORT_REVENUE_EUR_MWH(P) < gas_eur  =>  P < gas_eur + export_fee_eur_mwh
    direct_pv_boiler_vs_export_break_even_spot_eur_mwh = gas_eur + export_fee_eur_mwh

    return {
        "fixed_import_eur_mwh": fixed_import_eur_mwh,
        "export_fee_eur_mwh": export_fee_eur_mwh,
        "import_zero_spot_eur_mwh": import_zero_spot_eur_mwh,
        "self_use_spot_delta_eur_mwh": self_use_spot_delta_eur_mwh,
        "grid_export_formula_constant": grid_export_formula_constant,
        "direct_grid_boiler_break_even_spot_eur_mwh": direct_grid_boiler_break_even_spot_eur_mwh,
        "direct_pv_boiler_vs_export_break_even_spot_eur_mwh": direct_pv_boiler_vs_export_break_even_spot_eur_mwh,
    }
