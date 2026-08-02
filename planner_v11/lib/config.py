"""
Parser a validátor konfigurace pro planner v10.

Autoritativní zdroj pravidel: ARCHITECTURE_DESIGN_v10_FINAL.md sekce 4,
CONTROL_LOGIC_SPEC_v10.yaml a SERVER_IMPLEMENTATION_GUIDE_v10.md sekce 3/5.

Zásady (MUST):
  - config.toml se čte a validuje při KAŽDÉM běhu (planner/executor jsou
    stateless cron úlohy - žádné cachování mezi procesy).
  - Neznámý klíč i chybějící povinný klíč jsou OBA chybou (žádné tiché
    přehlédnutí typo v konfiguraci).
  - Rozsahy a vzájemné návaznosti (např. min_soc <= max_soc_grid <=
    max_soc_pv) se ověřují explicitně.
  - Výsledná Config je frozen (immutable) - žádný kód po načtení nemůže
    konfiguraci za běhu procesu změnit.
  - Config nese config_hash (sha256 nad kanonickým JSON obsahem) - zapisuje
    se do každého forecast_48h.json, aby šlo dohledat, s jakou konfigurací
    byl plán spočítán.

Použití:
    from lib.config import load_config, ConfigError
    cfg = load_config("/home/automatization/goodwe/planner/config.toml")
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Union

PathLike = Union[str, "Path"]

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_PHASES = ("L1", "L2", "L3")


class ConfigError(Exception):
    """Vyvolána při neplatné/neúplné konfiguraci.

    Obsahuje VŠECHNY nalezené chyby najednou (ne jen první), aby šlo
    konfiguraci opravit na jeden zátah."""

    def __init__(self, errors: list[str], path: str | None = None):
        self.errors = list(errors)
        self.path = path
        where = f" ({path})" if path else ""
        msg = f"Neplatná konfigurace{where}:\n  - " + "\n  - ".join(self.errors)
        super().__init__(msg)


# ============================================================================
# Datové třídy - jedna na sekci TOML, viz config.toml.example / architektura.
# ============================================================================

@dataclass(frozen=True)
class SystemConfig:
    timezone: str
    planning_step_minutes: int
    execution_step_minutes: int
    horizon_hours: int
    plan_max_age_minutes: float
    dry_run: bool
    battery_write_enabled: bool
    boiler_write_enabled: bool
    whatsapp_listener_enabled: bool

    @property
    def horizon_slots(self) -> int:
        return self.horizon_hours * 60 // self.planning_step_minutes


@dataclass(frozen=True)
class EconomicsConfig:
    czk_per_eur: float
    purchase_fixed_czk_per_mwh_pre_vat: float
    purchase_vat_multiplier: float
    export_service_czk_per_mwh: float
    export_disable_spot_eur_mwh: float
    battery_cycle_cost_eur_per_mwh: float
    battery_cycle_cost_model: str
    gas_heat_value_eur_per_mwh: float
    estimated_price_fallback_margin_eur_per_mwh: float


@dataclass(frozen=True)
class BatteryConfig:
    capacity_kwh: float
    min_soc_pct: float
    max_soc_pv_pct: float
    max_soc_grid_pct: float
    max_charge_kw: float
    max_discharge_kw: float
    terminal_value_lookahead_hours: float
    hold_power_pct: float


@dataclass(frozen=True)
class GridConfig:
    main_breaker_a: float
    soft_phase_limit_a: float
    phase_nominal_voltage_v: float
    inverter_total_kw: float


@dataclass(frozen=True)
class EvConfig:
    phase: str
    nominal_power_kw: float
    planning_power_kw: float
    taper_reserve_minutes: float
    interruptible: bool
    wallbox_energy_correction: float


@dataclass(frozen=True)
class PoolConfig:
    flow_phase: str
    flow_power_kw: float
    morning_start: str
    morning_end: str
    afternoon_start: str
    afternoon_end: str
    missing_alert_delay_minutes: float
    offseason_confirm_days: float
    startup_absence_days: float
    continuous_override_extra_minutes: float
    heat_pump_phase: str
    heat_pump_max_kw: float
    profile_weights: tuple[float, ...]


@dataclass(frozen=True)
class BoilerConfig:
    phase_power_kw: float
    phase_count: int
    execution_quantum_minutes: float
    initial_full_heat_max_kwh: float
    initial_full_heat_max_hours: float
    opportunistic_daily_limit_kwh: float
    relay_verify_delay_seconds: float
    minimum_on_minutes: float
    minimum_off_minutes: float
    rebalance_hysteresis_kw: float


@dataclass(frozen=True)
class AlertsConfig:
    daily_report_time: str
    fault_repeat_minutes: float
    pool_repeat_hours: float


@dataclass(frozen=True)
class SolverConfig:
    time_limit_seconds: float
    mip_gap: float
    economic_tie_tolerance_czk: float


@dataclass(frozen=True)
class Config:
    system: SystemConfig
    economics: EconomicsConfig
    battery: BatteryConfig
    grid: GridConfig
    ev: EvConfig
    pool: PoolConfig
    boiler: BoilerConfig
    alerts: AlertsConfig
    solver: SolverConfig
    config_hash: str
    source_path: str


# ============================================================================
# Validační framework - malý, bezúčelová závislost na knihovnách navíc.
# ============================================================================

class _SectionValidator:
    """Nasbírá chyby pro jednu sekci a poskytne typované gettery hodnot."""

    def __init__(self, section_name: str, raw_section: Any, errors: list[str]):
        self.section_name = section_name
        self.errors = errors
        self.data: dict = raw_section if isinstance(raw_section, dict) else {}
        if not isinstance(raw_section, dict):
            self.errors.append(f"[{section_name}] chybí celá sekce nebo není tabulka")
        self._seen_keys: set[str] = set()

    def _err(self, key: str, msg: str) -> None:
        self.errors.append(f"[{self.section_name}.{key}] {msg}")

    def get(
        self,
        key: str,
        expected_type: type | tuple[type, ...],
        *,
        min_value: float | None = None,
        max_value: float | None = None,
        allowed: tuple | None = None,
        validator: Callable[[Any], str | None] | None = None,
    ) -> Any:
        """Vrátí hodnotu klíče a zaznamená ji jako 'viděnou' pro detekci
        neznámých klíčů. Při chybě zapíše popis a vrátí None (validace
        pokračuje dál, aby se posbíraly všechny chyby najednou)."""
        self._seen_keys.add(key)
        if key not in self.data:
            self._err(key, "povinná hodnota chybí")
            return None
        value = self.data[key]

        # bool je v Pythonu podtřída int - pokud chceme skutečný int/float,
        # bool musí explicitně selhat, pokud int/float není v expected_type.
        if expected_type in (int, float, (int, float)) and isinstance(value, bool):
            self._err(key, f"musí být číslo, ne bool ({value!r})")
            return None

        if not isinstance(value, expected_type):
            self._err(key, f"musí být typu {expected_type}, je {type(value).__name__} ({value!r})")
            return None

        if min_value is not None and value < min_value:
            self._err(key, f"musí být >= {min_value}, je {value}")
        if max_value is not None and value > max_value:
            self._err(key, f"musí být <= {max_value}, je {value}")
        if allowed is not None and value not in allowed:
            self._err(key, f"musí být jedna z {allowed}, je {value!r}")
        if validator is not None:
            problem = validator(value)
            if problem:
                self._err(key, problem)

        return value

    def unknown_keys(self) -> list[str]:
        return sorted(set(self.data.keys()) - self._seen_keys)


def _hhmm_or_error(value: str) -> str | None:
    if not _HHMM_RE.match(value):
        return f"musí být ve formátu HH:MM (24h), je {value!r}"
    return None


def _hhmm_to_minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


REQUIRED_TOP_SECTIONS = (
    "system", "economics", "battery", "grid", "ev", "pool", "boiler",
    "alerts", "solver",
)


def _validate_and_build(raw: dict) -> tuple[Config | None, list[str]]:
    errors: list[str] = []

    unknown_sections = sorted(set(raw.keys()) - set(REQUIRED_TOP_SECTIONS))
    for s in unknown_sections:
        errors.append(f"neznámá sekce [{s}] v konfiguraci")
    for s in REQUIRED_TOP_SECTIONS:
        if s not in raw:
            errors.append(f"chybí povinná sekce [{s}]")

    # --- system ---
    v = _SectionValidator("system", raw.get("system"), errors)
    system = SystemConfig(
        timezone=v.get("timezone", str) or "Europe/Prague",
        planning_step_minutes=v.get("planning_step_minutes", int, min_value=1, max_value=60) or 15,
        execution_step_minutes=v.get("execution_step_minutes", int, min_value=1, max_value=60) or 5,
        horizon_hours=v.get("horizon_hours", int, min_value=1, max_value=168) or 48,
        plan_max_age_minutes=v.get("plan_max_age_minutes", (int, float), min_value=1) or 90,
        dry_run=v.get("dry_run", bool),
        battery_write_enabled=v.get("battery_write_enabled", bool),
        boiler_write_enabled=v.get("boiler_write_enabled", bool),
        whatsapp_listener_enabled=v.get("whatsapp_listener_enabled", bool),
    )
    for k in v.unknown_keys():
        errors.append(f"[system.{k}] neznámý klíč")

    # --- economics ---
    v = _SectionValidator("economics", raw.get("economics"), errors)
    economics = EconomicsConfig(
        czk_per_eur=v.get("czk_per_eur", (int, float), min_value=0.01) or 24.60,
        purchase_fixed_czk_per_mwh_pre_vat=v.get(
            "purchase_fixed_czk_per_mwh_pre_vat", (int, float), min_value=0) or 0.0,
        purchase_vat_multiplier=v.get("purchase_vat_multiplier", (int, float), min_value=1.0) or 1.0,
        export_service_czk_per_mwh=v.get("export_service_czk_per_mwh", (int, float), min_value=0) or 0.0,
        export_disable_spot_eur_mwh=v.get("export_disable_spot_eur_mwh", (int, float)) or 0.0,
        battery_cycle_cost_eur_per_mwh=v.get(
            "battery_cycle_cost_eur_per_mwh", (int, float), min_value=0) or 0.0,
        battery_cycle_cost_model=v.get("battery_cycle_cost_model", str) or "",
        gas_heat_value_eur_per_mwh=v.get("gas_heat_value_eur_per_mwh", (int, float), min_value=0) or 0.0,
        estimated_price_fallback_margin_eur_per_mwh=v.get(
            "estimated_price_fallback_margin_eur_per_mwh", (int, float), min_value=0) or 0.0,
    )
    for k in v.unknown_keys():
        errors.append(f"[economics.{k}] neznámý klíč")

    # --- battery ---
    v = _SectionValidator("battery", raw.get("battery"), errors)
    battery = BatteryConfig(
        capacity_kwh=v.get("capacity_kwh", (int, float), min_value=0.1) or 1.0,
        min_soc_pct=v.get("min_soc_pct", (int, float), min_value=0, max_value=100) or 0.0,
        max_soc_pv_pct=v.get("max_soc_pv_pct", (int, float), min_value=0, max_value=100) or 100.0,
        max_soc_grid_pct=v.get("max_soc_grid_pct", (int, float), min_value=0, max_value=100) or 100.0,
        max_charge_kw=v.get("max_charge_kw", (int, float), min_value=0.01) or 1.0,
        max_discharge_kw=v.get("max_discharge_kw", (int, float), min_value=0.01) or 1.0,
        terminal_value_lookahead_hours=v.get(
            "terminal_value_lookahead_hours", (int, float), min_value=0) or 0.0,
        hold_power_pct=v.get("hold_power_pct", (int, float), min_value=0, max_value=100) or 0.0,
    )
    for k in v.unknown_keys():
        errors.append(f"[battery.{k}] neznámý klíč")
    if not errors_contain(errors, "battery."):
        if battery.min_soc_pct > battery.max_soc_grid_pct:
            errors.append("[battery] min_soc_pct musí být <= max_soc_grid_pct")
        if battery.max_soc_grid_pct > battery.max_soc_pv_pct:
            errors.append("[battery] max_soc_grid_pct musí být <= max_soc_pv_pct")

    # --- grid ---
    v = _SectionValidator("grid", raw.get("grid"), errors)
    grid = GridConfig(
        main_breaker_a=v.get("main_breaker_a", (int, float), min_value=0.1) or 32.0,
        soft_phase_limit_a=v.get("soft_phase_limit_a", (int, float), min_value=0.1) or 30.0,
        phase_nominal_voltage_v=v.get("phase_nominal_voltage_v", (int, float), min_value=1) or 230.0,
        inverter_total_kw=v.get("inverter_total_kw", (int, float), min_value=0.1) or 10.0,
    )
    for k in v.unknown_keys():
        errors.append(f"[grid.{k}] neznámý klíč")
    if not errors_contain(errors, "grid."):
        if grid.soft_phase_limit_a > grid.main_breaker_a:
            errors.append("[grid] soft_phase_limit_a musí být <= main_breaker_a")

    # --- ev ---
    v = _SectionValidator("ev", raw.get("ev"), errors)
    ev = EvConfig(
        phase=v.get("phase", str, allowed=_PHASES) or "L1",
        nominal_power_kw=v.get("nominal_power_kw", (int, float), min_value=0.01) or 1.0,
        planning_power_kw=v.get("planning_power_kw", (int, float), min_value=0.01) or 1.0,
        taper_reserve_minutes=v.get("taper_reserve_minutes", (int, float), min_value=0) or 0.0,
        interruptible=v.get("interruptible", bool),
        wallbox_energy_correction=v.get("wallbox_energy_correction", (int, float), min_value=0.01) or 1.0,
    )
    for k in v.unknown_keys():
        errors.append(f"[ev.{k}] neznámý klíč")
    if not errors_contain(errors, "ev."):
        if ev.planning_power_kw > ev.nominal_power_kw:
            errors.append("[ev] planning_power_kw musí být <= nominal_power_kw (konzervativní plánování)")

    # --- pool ---
    v = _SectionValidator("pool", raw.get("pool"), errors)
    pool = PoolConfig(
        flow_phase=v.get("flow_phase", str, allowed=_PHASES) or "L3",
        flow_power_kw=v.get("flow_power_kw", (int, float), min_value=0) or 0.0,
        morning_start=v.get("morning_start", str, validator=_hhmm_or_error) or "09:30",
        morning_end=v.get("morning_end", str, validator=_hhmm_or_error) or "12:30",
        afternoon_start=v.get("afternoon_start", str, validator=_hhmm_or_error) or "13:30",
        afternoon_end=v.get("afternoon_end", str, validator=_hhmm_or_error) or "16:30",
        missing_alert_delay_minutes=v.get("missing_alert_delay_minutes", (int, float), min_value=0) or 30.0,
        offseason_confirm_days=v.get("offseason_confirm_days", (int, float), min_value=0) or 1.0,
        startup_absence_days=v.get("startup_absence_days", (int, float), min_value=0) or 7.0,
        continuous_override_extra_minutes=v.get(
            "continuous_override_extra_minutes", (int, float), min_value=0) or 30.0,
        heat_pump_phase=v.get("heat_pump_phase", str, allowed=_PHASES) or "L1",
        heat_pump_max_kw=v.get("heat_pump_max_kw", (int, float), min_value=0) or 2.0,
        profile_weights=tuple(v.get("profile_weights", list) or [0.6, 0.3, 0.1]),
    )
    for k in v.unknown_keys():
        errors.append(f"[pool.{k}] neznámý klíč")
    if not errors_contain(errors, "pool."):
        for label, start, end in (
            ("morning", pool.morning_start, pool.morning_end),
            ("afternoon", pool.afternoon_start, pool.afternoon_end),
        ):
            if _hhmm_to_minutes(start) >= _hhmm_to_minutes(end):
                errors.append(f"[pool] {label}_start musí být před {label}_end")
        if len(pool.profile_weights) != 3:
            errors.append("[pool.profile_weights] musí obsahovat přesně 3 hodnoty (den-1, den-2, den-3)")
        elif abs(sum(pool.profile_weights) - 1.0) > 1e-6:
            errors.append(f"[pool.profile_weights] musí sečíst na 1.0, sečteno {sum(pool.profile_weights)}")
        elif any(w < 0 for w in pool.profile_weights):
            errors.append("[pool.profile_weights] všechny váhy musí být >= 0")

    # --- boiler ---
    v = _SectionValidator("boiler", raw.get("boiler"), errors)
    boiler = BoilerConfig(
        phase_power_kw=v.get("phase_power_kw", (int, float), min_value=0.01) or 2.0,
        phase_count=v.get("phase_count", int, min_value=1, max_value=3) or 3,
        execution_quantum_minutes=v.get("execution_quantum_minutes", (int, float), min_value=1) or 5.0,
        initial_full_heat_max_kwh=v.get("initial_full_heat_max_kwh", (int, float), min_value=0.1) or 15.0,
        initial_full_heat_max_hours=v.get("initial_full_heat_max_hours", (int, float), min_value=0.1) or 2.5,
        opportunistic_daily_limit_kwh=v.get("opportunistic_daily_limit_kwh", (int, float), min_value=0) or 0.0,
        relay_verify_delay_seconds=v.get("relay_verify_delay_seconds", (int, float), min_value=0) or 2.0,
        minimum_on_minutes=v.get("minimum_on_minutes", (int, float), min_value=0) or 5.0,
        minimum_off_minutes=v.get("minimum_off_minutes", (int, float), min_value=0) or 5.0,
        rebalance_hysteresis_kw=v.get("rebalance_hysteresis_kw", (int, float), min_value=0) or 0.0,
    )
    for k in v.unknown_keys():
        errors.append(f"[boiler.{k}] neznámý klíč")

    # --- alerts ---
    v = _SectionValidator("alerts", raw.get("alerts"), errors)
    alerts = AlertsConfig(
        daily_report_time=v.get("daily_report_time", str, validator=_hhmm_or_error) or "06:00",
        fault_repeat_minutes=v.get("fault_repeat_minutes", (int, float), min_value=1) or 60.0,
        pool_repeat_hours=v.get("pool_repeat_hours", (int, float), min_value=1) or 24.0,
    )
    for k in v.unknown_keys():
        errors.append(f"[alerts.{k}] neznámý klíč")

    # --- solver ---
    v = _SectionValidator("solver", raw.get("solver"), errors)
    solver = SolverConfig(
        time_limit_seconds=v.get("time_limit_seconds", (int, float), min_value=0.1) or 30.0,
        mip_gap=v.get("mip_gap", (int, float), min_value=0) or 0.001,
        economic_tie_tolerance_czk=v.get("economic_tie_tolerance_czk", (int, float), min_value=0) or 0.0,
    )
    for k in v.unknown_keys():
        errors.append(f"[solver.{k}] neznámý klíč")

    if errors:
        return None, errors

    config_hash = _compute_hash(raw)
    cfg = Config(
        system=system, economics=economics, battery=battery, grid=grid,
        ev=ev, pool=pool, boiler=boiler, alerts=alerts, solver=solver,
        config_hash=config_hash, source_path="",
    )
    return cfg, []


def errors_contain(errors: list[str], prefix: str) -> bool:
    return any(prefix in e for e in errors)


def _compute_hash(raw: dict) -> str:
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_config_dict(raw: dict) -> Config:
    """Validuje a sestaví Config z už načteného dict (např. v testech).
    Vyvolá ConfigError se seznamem VŠECH chyb, pokud je konfigurace neplatná."""
    cfg, errors = _validate_and_build(raw)
    if errors or cfg is None:
        raise ConfigError(errors)
    return cfg


def load_config(path: PathLike) -> Config:
    """Načte a validuje config.toml ze zadané cesty. MUST se volat znovu
    při každém běhu planner.py/executor.py (žádné cachování mezi procesy)."""
    path = str(path)
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError([f"konfigurační soubor nenalezen: {path}"], path=path) from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"neplatný TOML syntax: {e}"], path=path) from e

    cfg, errors = _validate_and_build(raw)
    if errors or cfg is None:
        raise ConfigError(errors, path=path)

    # dataclass je frozen, ale source_path chceme doplnit - obejdeme přes
    # object.__setattr__ (bezpečné, protože config_hash je počítán z 'raw',
    # source_path do hashe nevstupuje).
    object.__setattr__(cfg, "source_path", path)
    return cfg
