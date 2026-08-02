"""
Pomocné plánovací funkce pro bojler (hard deadline odhad) pro planner v10.

Autoritativní zdroj pravidel:
  - ARCHITECTURE_DESIGN_v10_FINAL.md sekce 8.2 ("Tvrdý požadavek na
    bojler") a sekce 12 (tam, kde zmiňuje probe-and-observe - JEN pro
    kontext, samotná real-time logika je mimo rozsah tohoto modulu).
  - CONTROL_LOGIC_SPEC_v10.yaml sekce `boiler` (initial_full_heat_max_kwh
    15.0 / initial_full_heat_max_hours 2.5, hard_request).

Rozsah tohoto modulu (MUST):
  - Odhad ZBÝVAJÍCÍ energie potřebné do hard deadline
    (`remaining_required_kwh_for_hard_request`) - konzervativně až
    `initial_full_heat_max_kwh` (15 kWh), sníženo o energii již
    skutečně dodanou od vzniku požadavku (ARCH 8.2 "snižuje zbývající
    odhad podle skutečně detekovaného topení").
  - Sestavení `optimizer.BoilerHardRequest` (deadline_idx +
    required_kwh) pro daný horizont slotů - PLÁNOVACÍ odhad, který
    zavolá `planner.py` před voláním `lib.optimizer.optimize`.

Vědomá zjednodušení pro v1 (dokumentovaná, ne skryté zkratky):
  - Neexistuje teplotní senzor (`CONTROL_LOGIC_SPEC_v10.yaml
    boiler.temperature_sensor_available: false`) - "skutečně dodaná
    energie" musí volající dodat jako parametr (odvozeno executorem
    z detekce fázového odběru ~2kW, mimo rozsah TOHOTO modulu, který
    je čistě výpočetní/bezstavový).
  - `deadline_idx` se hledá jako POSLEDNÍ slot se `slot_start <=
    deadline` v daném seznamu slotů - pokud deadline leží MIMO
    horizont (za posledním slotem), vrátí se `None` (volající musí
    řešit jako "deadline mimo plánovací horizont", ARCH 8.2 fail-safe
    úvaha je mimo rozsah zde).

Mimo rozsah tohoto modulu:
  - probe-and-observe logika (real-time postupné přidávání fází) -
    executor (ARCH sekce 12.2, SERVER_IMPLEMENTATION_GUIDE_v10.md
    sekce 9).
  - Detekce termostatového vypnutí (relay_on AND ~2kW zmizí) -
    executor.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .optimizer import BoilerHardRequest


def remaining_required_kwh_for_hard_request(
    already_delivered_kwh: float, cfg: "Config"
) -> float:
    """Konzervativní odhad ZBÝVAJÍCÍ energie potřebné k naplnění tvrdého
    požadavku na bojler - `initial_full_heat_max_kwh` (výchozí 15 kWh)
    snížené o `already_delivered_kwh` (skutečně dodaná energie od vzniku
    požadavku, dodává volající/executor), ořezáno na >= 0 (ARCH 8.2)."""
    remaining = cfg.boiler.initial_full_heat_max_kwh - already_delivered_kwh
    return max(0.0, remaining)


def find_deadline_slot_idx(
    slot_starts: list[datetime], deadline: datetime, slot_minutes: int
) -> Optional[int]:
    """Najde index POSLEDNÍHO slotu, jehož INTERVAL [slot_start,
    slot_start + slot_minutes) ještě obsahuje `deadline`, nebo který
    celý předchází `deadline` (tzn. poslední slot, do jehož KONCE musí
    být energie dodána - `deadline_idx` je INCLUSIVE, viz
    `optimizer.BoilerHardRequest`).

    Vrátí None, pokud `deadline` leží PŘED prvním slotem, nebo pokud i
    poslední slot horizontu končí před `deadline` (deadline mimo
    horizont - volající musí řešit jako "mimo plánovací okno")."""
    if not slot_starts:
        return None
    from datetime import timedelta

    slot_len = timedelta(minutes=slot_minutes)
    if deadline < slot_starts[0]:
        return None

    last_idx = None
    for i, s in enumerate(slot_starts):
        if s < deadline:
            last_idx = i
        else:
            break
    if last_idx is None:
        return None
    # pokud deadline je presne na zacatku posledniho zahrnuteho slotu nebo
    # pozdeji nez konec horizontu, potvrdime, ze horizont deadline pokryva
    horizon_end = slot_starts[-1] + slot_len
    if deadline > horizon_end:
        return None
    return last_idx


def build_hard_request(
    slot_starts: list[datetime],
    deadline: datetime,
    already_delivered_kwh: float,
    cfg: "Config",
) -> Optional["BoilerHardRequest"]:
    """Sestaví `optimizer.BoilerHardRequest` pro daný horizont slotů, nebo
    None, pokud `deadline` leží mimo horizont (volající musí řešit
    fail-safe/notifikaci - mimo rozsah tohoto modulu)."""
    from .optimizer import BoilerHardRequest

    deadline_idx = find_deadline_slot_idx(
        slot_starts, deadline, cfg.system.planning_step_minutes
    )
    if deadline_idx is None:
        return None

    required_kwh = remaining_required_kwh_for_hard_request(already_delivered_kwh, cfg)
    if required_kwh <= 0:
        return None

    return BoilerHardRequest(deadline_idx=deadline_idx, required_kwh=required_kwh)
