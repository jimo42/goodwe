"""
Model základní spotřeby domu (base load) pro planner v10 - P50/P75 kvantily
per 15min slot dne, rozlišené pracovní den / víkend.

Autoritativní zdroj pravidel:
  - ARCHITECTURE_DESIGN_v10_FINAL.md sekce 7.1 ("Základní spotřeba domu")
    a sekce 7.4 ("Auto a detekce" - reziduální L1 prahy použité zde jen
    jako HRUBÁ offline aproximace, ne živý detektor s confidence).
  - CONTROL_LOGIC_SPEC_v10.yaml sekce `base_load` (history_days=30,
    expected_quantile=0.50, reserve_quantile=0.75,
    fallback_summer_overnight_reserve_kw=0.46).

Rozsah tohoto modulu (MUST):
  - Parsování textového formátu minutových reportů střídače (formát
    ověřen přímo na serveru, `logs/goodwe-reports/goodwe_stats_*`:
    řádky "klic: \t\t Popis = hodnota[ jednotka]").
  - HRUBÁ (offline, batch) exkluze rozpoznatelných zátěží (EV/bazénové
    oběhové čerpadlo/bojler) z naměřené celkové spotřeby domu, aby
    zbylý "base load" neobsahoval tyto řízené/ohlášené zátěže (ARCH
    7.1 "musí vyloučit rozpoznané: auto, bazén, bojler, ohlášené
    zátěže").
  - Agregace na 15min sloty (`profile_slot_minutes`) a výpočet P50
    (expected)/P75 (reserve) kvantilů přes konfigurovatelné okno
    historie (výchozí 30 dní), odděleně pro pracovní dny a víkendy.
  - Fallback `fallback_summer_overnight_reserve_kw` (~0.46 kW), pokud
    pro daný slot/den chybí dostatek historických dat - NENÍ to
    celoroční konstanta (ARCH 7.1), jen nouzová hodnota pro léto.

Vědomá zjednodušení pro v1 (dokumentovaná, ne skryté zkratky):
  - Exkluze EV/bazén/bojler je HRUBÁ prahová heuristika na JEDNOM
    vzorku (bez rolling window/confidence) - přesná detekce s
    confidence je úkol reálného executor detektoru (ARCH 7.3/7.4),
    tento modul jen co nejlépe očistí HISTORICKÁ data pro účely
    stavby profilu.
  - Svátky/prázdniny se v v1 NEROZLIŠUJÍ od pracovních dnů (jen
    Monday-Friday vs. Saturday/Sunday dle `datetime.weekday()`) - ARCH
    "split_weekday_weekend_when_data_sufficient" nezmiňuje svátky
    explicitně.
  - Kvantily počítány lineární interpolací (stejná konvence jako
    numpy.percentile default 'linear'), žádná závislost na numpy.

Mimo rozsah tohoto modulu:
  - Skutečné čtení/rozbalování archivů ze serveru
    (`logs/goodwe-reports/*.tgz`) - to dělá budoucí
    `update_base_load_profile.py` (cron skript), který zavolá funkce
    zde nad již načtenými cestami k jednotlivým report souborům.
  - Živá detekce EV/bazénu/bojleru v reálném čase - to je executor.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .config import Config

# APROXIMACE (ARCH sekce 7.4 / CONTROL_LOGIC_SPEC_v10.yaml vehicle.detector) -
# reziduální práh pro HRUBOU offline detekci EV na jednom vzorku. Živý
# detektor v executoru používá navíc rolling window (3 z 5 minut) a
# confidence - zde jen orientační jednorázový práh pro čištění historie.
EV_START_RESIDUAL_KW = 2.4

# APROXIMACE - tolerance pro rozpoznání bazénového oběhového čerpadla
# (pevná zátěž `cfg.pool.flow_power_kw`, typicky 0.55 kW) na jednom vzorku.
POOL_SIGNATURE_TOLERANCE_KW = 0.15

# APROXIMACE - tolerance pro rozpoznání bojlerového kroku (násobky
# `cfg.boiler.phase_power_kw`, typicky 2.0 kW) na jednom vzorku.
BOILER_STEP_TOLERANCE_KW = 0.3

_REQUIRED_RAW_KEYS = ("timestamp", "load_p1", "load_p2", "load_p3", "house_consumption")

_PHASE_KEY = {"L1": "load_p1", "L2": "load_p2", "L3": "load_p3"}


@dataclass(frozen=True)
class RawSample:
    """Jeden minutový vzorek z reportu střídače (jen relevantní pole)."""

    timestamp: datetime
    load_l1_kw: float
    load_l2_kw: float
    load_l3_kw: float
    house_consumption_kw: float

    def phase_kw(self, phase: str) -> float:
        return {"L1": self.load_l1_kw, "L2": self.load_l2_kw, "L3": self.load_l3_kw}[phase]


@dataclass(frozen=True)
class ExclusionBreakdown:
    """Odhad energie (kW) odečtené z `house_consumption_kw` per kategorie -
    jen pro diagnostiku/evidence, součet se odečítá v `excluded_load_kw`."""

    ev_kw: float
    pool_circulation_kw: float
    boiler_kw: float

    @property
    def total_kw(self) -> float:
        return self.ev_kw + self.pool_circulation_kw + self.boiler_kw


# ============================================================================
# Parsování textového formátu reportu
# ============================================================================

def parse_report_text(text: str) -> dict[str, str]:
    """Parsuje obsah jednoho `goodwe_stats_*` souboru na {klic: hodnota_str}.

    Formát řádku (ověřeno na serveru): 'klic: \\t\\t Popis = hodnota[ jednotka]'.
    Řádky bez '=' nebo bez ':' se tiše přeskočí (robustní čtení)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line or "=" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        if not key:
            continue
        value_part = line.split("=", 1)[1].strip()
        out[key] = value_part
    return out


def _parse_float(value_str: str) -> Optional[float]:
    if not value_str:
        return None
    number_text = value_str.split()[0]
    try:
        return float(number_text)
    except ValueError:
        return None


def _parse_timestamp(value_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value_str.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def extract_sample(raw: dict[str, str]) -> Optional[RawSample]:
    """Sestaví `RawSample` z parsovaného dict. Vrátí None, pokud chybí
    jakékoliv požadované pole nebo je nečíselné/neparsovatelné (robustní
    čtení, žádná tvrdá výjimka)."""
    if not all(k in raw for k in _REQUIRED_RAW_KEYS):
        return None
    ts = _parse_timestamp(raw["timestamp"])
    l1 = _parse_float(raw["load_p1"])
    l2 = _parse_float(raw["load_p2"])
    l3 = _parse_float(raw["load_p3"])
    house = _parse_float(raw["house_consumption"])
    if ts is None or l1 is None or l2 is None or l3 is None or house is None:
        return None
    return RawSample(
        timestamp=ts,
        load_l1_kw=l1 / 1000.0,
        load_l2_kw=l2 / 1000.0,
        load_l3_kw=l3 / 1000.0,
        house_consumption_kw=house / 1000.0,
    )


def read_report_file(path: str) -> Optional[RawSample]:
    """Přečte a parsuje jeden report soubor ze zadané cesty. None, pokud
    soubor neexistuje/je nečitelný/nekompletní."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    return extract_sample(parse_report_text(text))


def load_samples_from_paths(paths: list[str]) -> list[RawSample]:
    """Přečte více report souborů ze zadaných cest, tiše přeskočí
    nenačitatelné/nekompletní soubory."""
    samples = []
    for p in paths:
        s = read_report_file(p)
        if s is not None:
            samples.append(s)
    return samples


# ============================================================================
# HRUBÁ (offline) exkluze EV/bazén/bojler z naměřené spotřeby
# ============================================================================

def estimate_exclusion(sample: RawSample, cfg: "Config") -> ExclusionBreakdown:
    """Odhadne, kolik z `sample.house_consumption_kw` pravděpodobně patří
    EV/bazénovému oběhovému čerpadlu/bojleru na základě HRUBÝCH prahů na
    tomto jednom vzorku (viz docstring modulu - vědomé zjednodušení)."""
    ev_phase_kw = sample.phase_kw(cfg.ev.phase)
    ev_kw = min(ev_phase_kw, cfg.ev.planning_power_kw) if ev_phase_kw >= EV_START_RESIDUAL_KW else 0.0

    pool_phase_kw = sample.phase_kw(cfg.pool.flow_phase)
    pool_kw = (
        cfg.pool.flow_power_kw
        if abs(pool_phase_kw - cfg.pool.flow_power_kw) <= POOL_SIGNATURE_TOLERANCE_KW
        else 0.0
    )

    boiler_kw = 0.0
    phase_power = cfg.boiler.phase_power_kw
    if phase_power > 0:
        for phase in ("L1", "L2", "L3"):
            phase_kw = sample.phase_kw(phase)
            # kolik nejblizsich nasobku phase_power_kw se vejde do phase_kw
            steps = round(phase_kw / phase_power)
            if steps > 0 and abs(phase_kw - steps * phase_power) <= BOILER_STEP_TOLERANCE_KW:
                boiler_kw += steps * phase_power

    return ExclusionBreakdown(ev_kw=ev_kw, pool_circulation_kw=pool_kw, boiler_kw=boiler_kw)


def excluded_load_kw(sample: RawSample, cfg: "Config") -> float:
    """`house_consumption_kw` očištěná od HRUBĚ rozpoznaného EV/bazénu/
    bojleru, ořezáno na >= 0."""
    exclusion = estimate_exclusion(sample, cfg)
    return max(0.0, sample.house_consumption_kw - exclusion.total_kw)


# ============================================================================
# Agregace na profil P50/P75 per slot dne (weekday/weekend)
# ============================================================================

def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5=Saturday, 6=Sunday


def slot_key_for(dt: datetime, slot_minutes: int) -> str:
    """Klíč slotu profilu: '<weekday|weekend>_<HH:MM>' (HH:MM zaokrouhleno
    dolů na `slot_minutes`)."""
    daytype = "weekend" if _is_weekend(dt.date()) else "weekday"
    minute = (dt.minute // slot_minutes) * slot_minutes
    return f"{daytype}_{dt.hour:02d}:{minute:02d}"


def _percentile(sorted_values: list[float], q: float) -> float:
    """Lineární interpolace kvantilu (konvence shodná s numpy.percentile
    'linear', ale bez závislosti na numpy)."""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    idx = q * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


@dataclass(frozen=True)
class SlotProfile:
    expected_kw: float
    reserve_kw: float
    sample_count: int


def build_profile(
    samples: list[RawSample],
    cfg: "Config",
    reference_date: date,
    history_days: int = 30,
    expected_quantile: float = 0.50,
    reserve_quantile: float = 0.75,
) -> dict[str, SlotProfile]:
    """Sestaví profil {slot_key: SlotProfile} z historických vzorků v okně
    [reference_date - history_days, reference_date) - viz ARCH 7.1
    "doporučené okno historie 30 dní"."""
    slot_minutes = cfg.system.planning_step_minutes
    cutoff_start = reference_date - timedelta(days=history_days)

    buckets: dict[str, list[float]] = {}
    for s in samples:
        d = s.timestamp.date()
        if d < cutoff_start or d >= reference_date:
            continue
        key = slot_key_for(s.timestamp, slot_minutes)
        buckets.setdefault(key, []).append(excluded_load_kw(s, cfg))

    profile: dict[str, SlotProfile] = {}
    for key, values in buckets.items():
        values.sort()
        profile[key] = SlotProfile(
            expected_kw=_percentile(values, expected_quantile),
            reserve_kw=_percentile(values, reserve_quantile),
            sample_count=len(values),
        )
    return profile


def expected_load_kwh_for_slot(
    profile: dict[str, SlotProfile],
    dt: datetime,
    slot_minutes: int,
    fallback_overnight_reserve_kw: float,
    min_samples: int = 3,
) -> tuple[float, float, str]:
    """Vrátí (expected_kwh, reserve_kwh, source) pro daný slot.

    `source` je 'profile', pokud má slot dost vzorků (>= `min_samples`),
    jinak 'fallback_overnight_reserve' (ARCH 7.1 - fallback pro letní
    noc, NENÍ celoroční konstanta - volající by měl fallback používat
    hlavně pro noční sloty)."""
    slot_hours = slot_minutes / 60.0
    key = slot_key_for(dt, slot_minutes)
    slot = profile.get(key)
    if slot is not None and slot.sample_count >= min_samples:
        return slot.expected_kw * slot_hours, slot.reserve_kw * slot_hours, "profile"
    fallback_kwh = fallback_overnight_reserve_kw * slot_hours
    return fallback_kwh, fallback_kwh, "fallback_overnight_reserve"
