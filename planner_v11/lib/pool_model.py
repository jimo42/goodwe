"""
Bazénový profil a stavový automat pro planner v10.

Autoritativní zdroj pravidel:
  - ARCHITECTURE_DESIGN_v10_FINAL.md sekce 7.2 ("Bazénový profil") a 7.3
    ("Stavový automat bazénu").
  - CONTROL_LOGIC_SPEC_v10.yaml sekce `pool` (schedule, circulation,
    heat_pump, state_machine).

Rozsah tohoto modulu (MUST):
  - Pevná okna oběhového čerpadla (`morning_start/end`,
    `afternoon_start/end`) jako FIXNÍ zátěž `flow_power_kw` na
    `flow_phase` (ARCH 7.2 "oběhové čerpadlo je pevně vložená zátěž").
  - Relativní model tepelného čerpadla vážený `profile_weights`
    (výchozí 60/30/10 % za poslední 3 dny), zarovnaný RELATIVNĚ od
    začátku cyklu (ne absolutním časem dne) - ARCH 7.2.
  - Stavový automat s pěti stavy (UNKNOWN/OFF_SEASON/NORMAL_OPERATION/
    CONTINUOUS_OVERRIDE/EXPECTED_CYCLE_MISSING) a přechodovými
    pravidly dle ARCH 7.3.

Vědomá zjednodušení pro v1 (dokumentovaná, ne skryté zkratky):
  - Detekce signatury oběhového čerpadla (~0.55 kW na `flow_phase`) je
    prahová heuristika na JEDNOM vzorku - stejná konvence jako
    `lib/load_model.py` (žádné rolling window/confidence zde, to je
    úkol reálného executor detektoru, ARCH 7.3).
  - Tepelné čerpadlo se čistí od EV/bojleru/krátkých špiček ODEČTENÍM
    stejné HRUBÉ heuristiky jako `lib/load_model.estimate_exclusion`
    (aby nedocházelo k duplikaci jiné logiky) a ořízne na [0, 2] kW dle
    ARCH 7.2 "ořízne na 0-2 kW".
  - Pro DRUHÝ den horizontu (48h dopředu) se v1 použije STEJNÝ profil
    jako pro první den (ARCH 7.2 "pro druhý den horizontu se použije
    stejný profil, případně později teplotní korekce") - teplotní
    korekce je mimo rozsah v1.

Mimo rozsah tohoto modulu:
  - Živá detekce/alertování v reálném čase (30min missing alert,
    dedup, notifikace) - to řeší executor + `lib/notify.py`, zde jen
    ČISTÁ funkce `classify_state` nad již vypočtenými fakty o dni.
  - Skutečné čtení archivů z `logs/goodwe-reports/` - volající
    (budoucí `update_pool_model.py`) předá už načtené `RawSample` ze
    `lib/load_model.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Optional

from . import load_model

if TYPE_CHECKING:
    from .config import Config
    from .load_model import RawSample

# Stavy stavového automatu (ARCH sekce 7.3 / CONTROL_LOGIC_SPEC_v10.yaml
# pool.state_machine.states)
STATE_UNKNOWN = "UNKNOWN"
STATE_OFF_SEASON = "OFF_SEASON"
STATE_NORMAL_OPERATION = "NORMAL_OPERATION"
STATE_CONTINUOUS_OVERRIDE = "CONTINUOUS_OVERRIDE"
STATE_EXPECTED_CYCLE_MISSING = "EXPECTED_CYCLE_MISSING"

# Ořez profilu tepelného čerpadla (ARCH sekce 7.2 "ořízne na 0-2 kW").
HEAT_PUMP_PROFILE_MIN_KW = 0.0
HEAT_PUMP_PROFILE_MAX_KW = 2.0


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class PoolWindow:
    start: time
    end: time

    def contains(self, t: time) -> bool:
        return self.start <= t <= self.end


def morning_window(cfg: "Config") -> PoolWindow:
    return PoolWindow(_parse_hhmm(cfg.pool.morning_start), _parse_hhmm(cfg.pool.morning_end))


def afternoon_window(cfg: "Config") -> PoolWindow:
    return PoolWindow(_parse_hhmm(cfg.pool.afternoon_start), _parse_hhmm(cfg.pool.afternoon_end))


def is_in_any_window(dt: datetime, cfg: "Config") -> bool:
    t = dt.time()
    return morning_window(cfg).contains(t) or afternoon_window(cfg).contains(t)


def circulation_load_kwh_for_slot(dt: datetime, slot_minutes: int, cfg: "Config") -> float:
    """Fixní zátěž oběhového čerpadla pro daný slot - buď celý
    `flow_power_kw * slot_hours`, pokud je slot uvnitř okna, jinak 0.0
    (ARCH 7.2 - pevná zátěž, žádné částečné pokrytí slotu oknem v v1)."""
    if not is_in_any_window(dt, cfg):
        return 0.0
    slot_hours = slot_minutes / 60.0
    return cfg.pool.flow_power_kw * slot_hours


# ============================================================================
# Relativní profil tepelného čerpadla (ARCH 7.2)
# ============================================================================

def _heat_pump_kw_at(sample: "RawSample", cfg: "Config") -> float:
    """Odhad výkonu tepelného čerpadla v jednom vzorku - fáze
    `heat_pump_phase`, očištěná od HRUBĚ rozpoznaného EV/bojleru/bazénu
    (stejná heuristika jako `lib.load_model.estimate_exclusion`), ořízlá
    na [0, 2] kW."""
    phase_kw = sample.phase_kw(cfg.pool.heat_pump_phase)
    exclusion = load_model.estimate_exclusion(sample, cfg)
    # heat_pump_phase muze byt sdilena s EV/bojlerem - odecti jen tu cast
    # exkluze, ktera se tyka STEJNE faze jako heat pump.
    excluded_same_phase = 0.0
    if cfg.ev.phase == cfg.pool.heat_pump_phase:
        excluded_same_phase += exclusion.ev_kw
    if cfg.pool.flow_phase == cfg.pool.heat_pump_phase:
        excluded_same_phase += exclusion.pool_circulation_kw
    # boiler_kw v ExclusionBreakdown je souhrn pres vsechny faze - nelze
    # jednoznacne rozdelit per-fazi bez dalsich dat, proto se pro
    # heat_pump_phase odecte jen pomerna cast pripadajici na tu fazi
    # (konzervativni aproximace: pokud heat_pump_phase == "L1" jako
    # vychozi boiler faze 1, odecti az phase_power_kw).
    if cfg.boiler.phase_power_kw > 0:
        steps = round(phase_kw / cfg.boiler.phase_power_kw)
        if steps > 0 and abs(phase_kw - steps * cfg.boiler.phase_power_kw) <= load_model.BOILER_STEP_TOLERANCE_KW:
            excluded_same_phase += steps * cfg.boiler.phase_power_kw

    cleaned = phase_kw - excluded_same_phase
    return max(HEAT_PUMP_PROFILE_MIN_KW, min(HEAT_PUMP_PROFILE_MAX_KW, cleaned))


def build_heat_pump_cycle_profile(
    samples_by_day: dict[date, list["RawSample"]],
    cycle_start: time,
    cfg: "Config",
) -> dict[int, float]:
    """Sestaví relativní profil tepelného čerpadla {minuty_od_startu: kW}
    pro JEDEN den z `RawSample` seřazených podle `timestamp`, zarovnané
    relativně od `cycle_start` (ARCH 7.2 "profil se pro každý cyklus
    modeluje relativně od jeho začátku")."""
    profile: dict[int, float] = {}
    for d, samples in samples_by_day.items():
        cycle_dt = datetime.combine(d, cycle_start)
        for s in samples:
            if s.timestamp < cycle_dt:
                continue
            minute_offset = int((s.timestamp - cycle_dt).total_seconds() // 60)
            profile[minute_offset] = _heat_pump_kw_at(s, cfg)
    return profile


def weighted_heat_pump_forecast_kw(
    profiles_last_3_days: list[dict[int, float]],
    minute_offset: int,
    cfg: "Config",
) -> float:
    """Váženě zprůměruje odhad výkonu tepelného čerpadla v daném
    `minute_offset` od začátku cyklu přes až 3 poslední dny
    (`profile_weights`, výchozí 60/30/10 %, ARCH 7.2).

    `profiles_last_3_days[0]` = včerejší profil (nejvyšší váha), [1] =
    předvčerejší, [2] = třetí den. Chybějící dny se PŘESKOČÍ a váhy
    zbylých dnů se PŘEŠKÁLUJÍ tak, aby součet použitých vah byl 1.0."""
    weights = cfg.pool.profile_weights
    total_weight = 0.0
    weighted_sum = 0.0
    for i, profile in enumerate(profiles_last_3_days[: len(weights)]):
        if profile is None:
            continue
        kw = profile.get(minute_offset)
        if kw is None:
            continue
        w = weights[i]
        weighted_sum += kw * w
        total_weight += w
    if total_weight <= 0:
        return 0.0
    return weighted_sum / total_weight


# ============================================================================
# Stavový automat (ARCH 7.3)
# ============================================================================

@dataclass(frozen=True)
class DayCycleObservation:
    """Fakta o jednom kalendářním dni potřebná pro klasifikaci stavu -
    volající (budoucí `update_pool_model.py`) je sestaví z historických
    vzorků pomocí `circulation_detected_in_window`."""

    date: date
    morning_cycle_seen: bool
    afternoon_cycle_seen: bool
    morning_window_minutes_elapsed_without_signature: float
    afternoon_window_minutes_elapsed_without_signature: float
    run_outside_windows_detected: bool
    run_duration_exceeds_window_by_minutes: float


def circulation_detected_in_window(
    samples: list["RawSample"], window: PoolWindow, cfg: "Config"
) -> bool:
    """True, pokud alespoň jeden vzorek v daném okně nese signaturu
    oběhového čerpadla (~`flow_power_kw` na `flow_phase`, tolerance
    `load_model.POOL_SIGNATURE_TOLERANCE_KW`)."""
    for s in samples:
        if not window.contains(s.timestamp.time()):
            continue
        phase_kw = s.phase_kw(cfg.pool.flow_phase)
        if abs(phase_kw - cfg.pool.flow_power_kw) <= load_model.POOL_SIGNATURE_TOLERANCE_KW:
            return True
    return False


def classify_state(
    previous_state: str,
    today: DayCycleObservation,
    consecutive_days_without_pool: int,
    consecutive_days_with_stable_run: int,
    cfg: "Config",
) -> tuple[str, str]:
    """Odvodí nový stav + reason code z pozorování jednoho dne a
    předchozího stavu (ARCH 7.3 pravidla). Vrací (nový_stav, reason).

    Priorita pravidel (shora dolů, první splněné vyhrává):
      1. continuous_override - běh mimo okna nebo o >30min delší.
      2. expected_cycle_missing - >30min od startu očekávaného okna bez
         signatury.
      3. off_season - chybí oba cykly celý den (po
         `offseason_confirm_days` po sobě jdoucích dnech).
      4. season_started (-> NORMAL_OPERATION) - po >= `startup_absence_days`
         bez bazénu a novém stabilním běhu.
      5. normal_operation - default, pokud byly vidět očekávané cykly."""
    if today.run_outside_windows_detected or (
        today.run_duration_exceeds_window_by_minutes
        >= cfg.pool.continuous_override_extra_minutes
    ):
        return STATE_CONTINUOUS_OVERRIDE, "POOL_CONTINUOUS_OVERRIDE"

    if (
        not today.morning_cycle_seen
        and today.morning_window_minutes_elapsed_without_signature
        >= cfg.pool.missing_alert_delay_minutes
    ) or (
        not today.afternoon_cycle_seen
        and today.afternoon_window_minutes_elapsed_without_signature
        >= cfg.pool.missing_alert_delay_minutes
    ):
        return STATE_EXPECTED_CYCLE_MISSING, "POOL_CYCLE_MISSING"

    both_missing_today = not today.morning_cycle_seen and not today.afternoon_cycle_seen

    if previous_state == STATE_OFF_SEASON:
        if consecutive_days_with_stable_run >= 1 and (
            today.morning_cycle_seen or today.afternoon_cycle_seen
        ):
            return STATE_NORMAL_OPERATION, "POOL_SEASON_STARTED"
        return STATE_OFF_SEASON, "POOL_SEASON_STOPPED"

    if both_missing_today and consecutive_days_without_pool >= cfg.pool.offseason_confirm_days:
        return STATE_OFF_SEASON, "POOL_SEASON_STOPPED"

    if today.morning_cycle_seen or today.afternoon_cycle_seen:
        return STATE_NORMAL_OPERATION, "POOL_EXPECTED_LOAD"

    return previous_state if previous_state != STATE_UNKNOWN else STATE_UNKNOWN, "POOL_EXPECTED_LOAD"
