"""
Enumerace kandidátních souvislých EV nabíjecích intervalů pro planner v10.

Autoritativní zdroj pravidel:
  - ARCHITECTURE_DESIGN_v10_FINAL.md sekce 8.1 ("Nabíjení auta").
  - CONTROL_LOGIC_SPEC_v10.yaml sekce `vehicle` a `user_requests.types.ev_charge`.
  - SERVER_IMPLEMENTATION_GUIDE_v10.md sekce 11.3 ("EV bez automatického
    wallboxu").

Rozsah tohoto modulu (MUST):
  - "Wallbox není akční člen" (ARCH 10.2) - planner jen DOPORUČUJE jeden
    souvislý interval, nic automaticky nespíná.
  - Enumerace kandidátních souvislých startů po 15minutových krocích:
    interval musí začínat >= `available_from`, musí skončit před
    `deadline` s konzervativní `taper_reserve_minutes` rezervou
    (SERVER_IMPLEMENTATION_GUIDE_v10.md 11.3).
  - Pro každého kandidáta sestaví `optimizer.EvRequest` s indexy slotů
    odpovídajícími danému horizontu a zavolá poskytnutou hodnotící
    funkci (typicky `lib.optimizer.optimize`) - VOLAJÍCÍ dodává
    hodnotící funkci, tento modul neimportuje `lib.optimizer` přímo,
    aby nevznikl cyklický/zbytečně těsný vztah (viz `evaluate_candidates`
    docstring).
  - Vrátí `EvRecommendation` (doporučený start, nejpozdější bezpečný
    start, očekávaný konec, feasibilita) dle ARCH 8.1 výstupního formátu.

Vědomá zjednodušení pro v1 (dokumentovaná, ne skryté zkratky):
  - Enumerace zkouší KAŽDÝ možný start po 15min kroku v okně - pro
    typický horizont (48h) a `planning_power_kw` je to nanejvýš
    několik desítek kandidátů, plné MILP vyhodnocení každého by bylo
    drahé; SERVER_IMPLEMENTATION_GUIDE_v10.md 11.3 povoluje "zrychlené
    ocenění a nejlepší kandidáty ověřit úplným MILP" - tento modul
    implementuje JEDNODUCHÉ (levné) předběžné skórování
    (`_cheap_score_candidate`, průměrná cena importu v intervalu) pro
    seřazení kandidátů, a until `max_full_evaluations` z nejlepších
    nechá ověřit VOLAJÍCÍM (přesný MILP) - kompromis rychlost/přesnost
    zdokumentovaný explicitně, ne skrytý.
  - "Nejpozdější bezpečný start" se počítá čistě z časových oken
    (deadline - required_hours - taper_reserve), NE z MILP ekonomiky -
    je to fyzikální/časová hranice, ne ekonomické doporučení.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .optimizer import EvRequest, OptimizerResult


@dataclass(frozen=True)
class EvCandidate:
    """Jeden kandidátní souvislý interval nabíjení - indexy do seznamu
    slotů předaného volajícím (0-based, `end_idx` INCLUSIVE)."""

    start_idx: int
    end_idx: int
    start_time: datetime
    end_time: datetime
    cheap_score_czk_per_kwh: float


@dataclass(frozen=True)
class EvRecommendation:
    """Výstupní doporučení dle ARCH sekce 8.1."""

    feasible: bool
    recommended_start: Optional[datetime]
    latest_safe_start: Optional[datetime]
    expected_end: Optional[datetime]
    expected_delivered_kwh: float
    reason: str


def _slots_needed(required_ac_kwh: float, planning_power_kw: float, slot_hours: float) -> int:
    """Počet 15min slotů potřebných k dodání `required_ac_kwh` při
    `planning_power_kw` (zaokrouhleno nahoru - konzervativní, radši
    o slot víc)."""
    if planning_power_kw <= 0:
        return 0
    hours_needed = required_ac_kwh / planning_power_kw
    return max(1, math.ceil(hours_needed / slot_hours))


def slots_needed(required_ac_kwh: float, planning_power_kw: float, slot_hours: float) -> int:
    """Public conservative slot-count helper for a locked ongoing session."""

    return _slots_needed(required_ac_kwh, planning_power_kw, slot_hours)


def enumerate_candidates(
    slot_starts: list[datetime],
    available_from: datetime,
    deadline: datetime,
    required_ac_kwh: float,
    cfg: "Config",
    price_import_czk_kwh: list[float],
) -> list[EvCandidate]:
    """Enumeruje VŠECHNY kandidátní souvislé starty v `slot_starts`
    (seřazený seznam začátků slotů, typicky `[s.slot_start for s in slots]`),
    kde:
      - start slotu >= `available_from`,
      - konec intervalu (start + potřebný počet slotů) je <= `deadline`
        MÍNUS `taper_reserve_minutes` (konzervativní rezerva na pauzy/taper,
        ARCH 8.1 "plánovací výkon 2.75 kW a 15minutová rezerva již
        zohledňují pauzy a taper").

    `price_import_czk_kwh` musí mít STEJNOU délku jako `slot_starts` -
    použito jen pro levné předběžné skórování (`cheap_score_czk_per_kwh`),
    ne pro finální rozhodnutí (to dělá plné MILP u vybraných kandidátů)."""
    slot_hours = cfg.system.planning_step_minutes / 60.0
    n = len(slot_starts)
    if n == 0 or n != len(price_import_czk_kwh):
        return []

    needed_slots = _slots_needed(required_ac_kwh, cfg.ev.planning_power_kw, slot_hours)
    if needed_slots <= 0:
        return []

    taper_reserve = timedelta(minutes=cfg.ev.taper_reserve_minutes)
    latest_end_allowed = deadline - taper_reserve

    candidates: list[EvCandidate] = []
    for start_idx in range(n):
        start_time = slot_starts[start_idx]
        if start_time < available_from:
            continue
        end_idx = start_idx + needed_slots - 1
        if end_idx >= n:
            break
        end_time = slot_starts[end_idx] + timedelta(minutes=cfg.system.planning_step_minutes)
        if end_time > latest_end_allowed:
            continue

        window_prices = price_import_czk_kwh[start_idx : end_idx + 1]
        avg_price = sum(window_prices) / len(window_prices) if window_prices else 0.0

        candidates.append(
            EvCandidate(
                start_idx=start_idx,
                end_idx=end_idx,
                start_time=start_time,
                end_time=end_time,
                cheap_score_czk_per_kwh=avg_price,
            )
        )
    return candidates


def rank_candidates(candidates: list[EvCandidate]) -> list[EvCandidate]:
    """Seřadí kandidáty vzestupně dle `cheap_score_czk_per_kwh` (nejlevnější
    první) - levné předběžné skórování, viz docstring modulu."""
    return sorted(candidates, key=lambda c: c.cheap_score_czk_per_kwh)


def evaluate_candidates(
    candidates: list[EvCandidate],
    required_ac_kwh: float,
    cfg: "Config",
    evaluate_fn: Callable[["EvRequest"], "OptimizerResult"],
    max_full_evaluations: int = 5,
) -> tuple[Optional[EvCandidate], Optional["OptimizerResult"]]:
    """Vezme až `max_full_evaluations` nejlevnějších kandidátů (dle
    `rank_candidates`) a pro každého zavolá `evaluate_fn` (typicky
    `lib.optimizer.optimize` zabalené volajícím do jednoho argumentu
    `EvRequest -> OptimizerResult`) - vrátí kandidáta s NEJNIŽŠÍM
    `economic_objective_czk` mezi těmi, které mají nulový
    `ev_unserved_kwh` (plně proveditelné).

    Tento modul NEIMPORTUJE `lib.optimizer` přímo (viz docstring modulu)
    - `evaluate_fn` a `EvRequest` konstrukci zajišťuje volající."""
    from .optimizer import EvRequest  # pozdní import jen pro typovou konstrukci

    ranked = rank_candidates(candidates)[:max_full_evaluations]
    best_candidate = None
    best_result = None
    for c in ranked:
        req = EvRequest(
            window_start_idx=c.start_idx,
            window_end_idx=c.end_idx,
            required_ac_kwh=required_ac_kwh,
            planning_power_kw=cfg.ev.planning_power_kw,
        )
        result = evaluate_fn(req)
        if result.ev_unserved_kwh > 1e-6:
            continue
        if best_result is None or result.economic_objective_czk < best_result.economic_objective_czk:
            best_candidate = c
            best_result = result
    return best_candidate, best_result


def latest_safe_start(
    deadline: datetime, required_ac_kwh: float, cfg: "Config"
) -> datetime:
    """Nejpozdější bezpečný start = deadline - (potřebná doba nabíjení) -
    taper_reserve_minutes, čistě z časové/fyzikální bilance (ARCH 8.1),
    NE z MILP ekonomiky."""
    if cfg.ev.planning_power_kw <= 0:
        return deadline
    hours_needed = required_ac_kwh / cfg.ev.planning_power_kw
    return deadline - timedelta(hours=hours_needed) - timedelta(minutes=cfg.ev.taper_reserve_minutes)


def recommend(
    slot_starts: list[datetime],
    available_from: datetime,
    deadline: datetime,
    required_ac_kwh: float,
    cfg: "Config",
    price_import_czk_kwh: list[float],
    evaluate_fn: Callable[["EvRequest"], "OptimizerResult"],
    max_full_evaluations: int = 5,
) -> EvRecommendation:
    """Kompletní pipeline: enumerace -> levné skórování -> plné MILP
    ověření nejlepších kandidátů -> `EvRecommendation` (ARCH 8.1 výstupní
    formát: recommended_start/latest_safe_start/expected_end/
    expected_delivered_kwh/feasibility)."""
    safe_start = latest_safe_start(deadline, required_ac_kwh, cfg)

    candidates = enumerate_candidates(
        slot_starts, available_from, deadline, required_ac_kwh, cfg, price_import_czk_kwh
    )
    if not candidates:
        return EvRecommendation(
            feasible=False,
            recommended_start=None,
            latest_safe_start=safe_start,
            expected_end=None,
            expected_delivered_kwh=0.0,
            reason="Žádný fyzicky proveditelný interval v daném horizontu "
            "(deadline příliš blízko nebo required_ac_kwh příliš vysoké).",
        )

    best_candidate, best_result = evaluate_candidates(
        candidates, required_ac_kwh, cfg, evaluate_fn, max_full_evaluations
    )
    if best_candidate is None:
        # Žádný z ověřených kandidátů nebyl plně proveditelný (fyzikální
        # limity slotu/baterie) - vezmi nejlevnějšího kandidáta jako
        # best-effort doporučení, ale označ jako nefeasible.
        fallback = rank_candidates(candidates)[0]
        return EvRecommendation(
            feasible=False,
            recommended_start=fallback.start_time,
            latest_safe_start=safe_start,
            expected_end=fallback.end_time,
            expected_delivered_kwh=0.0,
            reason="Best-effort: žádný ověřený kandidát nedodal celou "
            "požadovanou energii (fyzikální limity), doporučen nejlevnější "
            "kandidát bez záruky plného dodání.",
        )

    return EvRecommendation(
        feasible=True,
        recommended_start=best_candidate.start_time,
        latest_safe_start=safe_start,
        expected_end=best_candidate.end_time,
        expected_delivered_kwh=required_ac_kwh,
        reason="Nalezen ekonomicky nejvýhodnější proveditelný interval "
        f"mezi ověřenými kandidáty (score {best_candidate.cheap_score_czk_per_kwh:.3f} CZK/kWh).",
    )
