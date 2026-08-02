"""
Načítání spotových cen energie (beze zápisu) pro planner v10.

Autoritativní zdroj pravidel: ARCHITECTURE_DESIGN_v10_FINAL.md sekce 6.1-6.3,
CONTROL_LOGIC_SPEC_v10.yaml sekce `price_forecast`.

Zdrojová data na serveru (ověřeno ssh průzkumem, viz lib/paths.py docstring):
    energy-prices/{YYYY-MM-DD}.csv        "HH:MM;cena_s_čárkou" (EUR/MWh)
    energy-prices/{YYYY-MM-DD}_check.csv  druhý zdroj (ENTSOE), fallback

Rozsah tohoto modulu (MUST):
  - Čtení a parsování "actual" (skutečně publikovaných) cen pro daný den.
    "Actual" ceny jsou VŽDY autoritativní a nahrazují jakýkoli odhad okamžitě
    (sekce 6.2/6.3 "actual_price_replaces_estimate_immediately: true").
  - Poskytnutí základních stavebních kamenů pro deterministický odhad
    (sekce 6.3) - `historical_median_eur_mwh` a `fallback_estimate_eur_mwh`
    implementují VÝSLOVNĚ zadaný nejhorší-případ fallback:
        import estimate = historical_median + estimated_price_fallback_margin_eur_per_mwh
        export estimate = historical_median - estimated_price_fallback_margin_eur_per_mwh
    (viz CONTROL_LOGIC_SPEC_v10.yaml `price_forecast.fallback`, config.toml
    `economics.estimated_price_fallback_margin_eur_per_mwh` = 30.00).

Co tento modul NEDĚLÁ (mimo rozsah, patří do budoucího
`update_price_estimator.py` / `state/price_estimator.json`):
  - Plný deterministický kvantilový model (p25/p50/p75) založený na
    čtvrthodině dne, dni v týdnu, sezóně, predikci PV a podobných
    historických dnech (sekce 6.3). Tento modul poskytuje jen ACTUAL data a
    prostý medián-based fallback jako stavební kámen, NE finální p25/p50/p75
    výstup.

Žádná funkce v tomto modulu nic nezapisuje do souborů ani zařízení.
"""
from __future__ import annotations

import os
import statistics
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Optional

from . import paths

if TYPE_CHECKING:
    from .config import Config


def _parse_price_csv_text(text: str) -> dict[str, float]:
    """Parsuje obsah CSV 'HH:MM;cena_s_čárkou' -> {'HH:MM': cena_float(EUR/MWh)}.

    Neplatné/nerozpoznané řádky se tiše přeskočí (robustní čtení, žádná
    tvrdá výjimka za běhu plannera/executoru kvůli poškozenému řádku).
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ";" not in line:
            continue
        hhmm, price_str = line.split(";", 1)
        hhmm = hhmm.strip()
        price_str = price_str.strip().replace(",", ".")
        try:
            out[hhmm] = float(price_str)
        except ValueError:
            continue
    return out


def read_price_csv(path: str) -> dict[str, float]:
    """Přečte jeden CSV soubor cen ze zadané cesty. Prázdný dict, pokud
    soubor neexistuje nebo je nečitelný (žádná výjimka - stateless cron
    proces musí umět běžet dál i bez tohoto konkrétního dne)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return _parse_price_csv_text(f.read())
    except OSError:
        return {}


# Minimální počet slotů (z 96 při 15min kroku), aby byl den považován za
# "dostatečně kompletní" actual den - stejná konzervativní hranice jako v
# předchozí v8 implementaci (viz remote_staging/planner/lib/prices.py).
MIN_COMPLETE_DAY_SLOTS = 90


def load_actual_day_prices(
    d: date, prices_dir: str = paths.PRICES_DIR
) -> Optional[dict[str, float]]:
    """Načte "actual" ceny pro daný den (primární soubor, fallback na
    `_check.csv` druhého zdroje). Vrátí None, pokud den není dostatečně
    kompletní (< MIN_COMPLETE_DAY_SLOTS z 96 slotů)."""
    day_str = d.strftime("%Y-%m-%d")
    primary = os.path.join(prices_dir, f"{day_str}.csv")
    prices = read_price_csv(primary)
    if len(prices) < MIN_COMPLETE_DAY_SLOTS:
        check = os.path.join(prices_dir, f"{day_str}_check.csv")
        prices_check = read_price_csv(check)
        if len(prices_check) > len(prices):
            prices = prices_check
    if len(prices) < MIN_COMPLETE_DAY_SLOTS:
        return None
    return prices


def get_actual_price_for_slot(
    dt: datetime, prices_dir: str = paths.PRICES_DIR
) -> Optional[float]:
    """Vrátí "actual" cenu (EUR/MWh) pro daný 15min slot (zaokrouhleno dolů
    na čtvrthodinu), nebo None, pokud actual cena pro tento slot není
    (ještě) k dispozici - VOLAJÍCÍ pak musí použít fallback odhad, actual
    cena se NIKDY nedopočítává/neinterpoluje zde."""
    day_prices = load_actual_day_prices(dt.date(), prices_dir)
    if day_prices is None:
        return None
    hhmm = f"{dt.hour:02d}:{(dt.minute // 15) * 15:02d}"
    return day_prices.get(hhmm)


def historical_median_eur_mwh(
    before: date,
    prices_dir: str = paths.PRICES_DIR,
    lookback_days: int = 30,
) -> Optional[float]:
    """Medián všech actual cen (EUR/MWh) ze všech DOSTUPNÝCH kompletních dnů
    v okně `lookback_days` dnů PŘED `before` (bez `before` samotného).

    Použito jako `base: historical_median` v CONTROL_LOGIC_SPEC_v10.yaml
    `price_forecast.fallback`. Vrátí None, pokud v okně není žádný kompletní
    den (např. čerstvá instalace bez historie)."""
    values: list[float] = []
    d = before - timedelta(days=1)
    for _ in range(lookback_days):
        day_prices = load_actual_day_prices(d, prices_dir)
        if day_prices is not None:
            values.extend(day_prices.values())
        d -= timedelta(days=1)
    if not values:
        return None
    return statistics.median(values)


def fallback_estimate_eur_mwh(
    median_eur_mwh: float, cfg: "Config"
) -> tuple[float, float]:
    """Vrátí (import_estimate, export_estimate) v EUR/MWh dle výslovného
    fallbacku ze sekce 6.3/CONTROL_LOGIC_SPEC_v10.yaml `price_forecast.fallback`,
    použitého POUZE pokud nelze vytvořit plný p25/p50/p75 kvantilový odhad:

        import estimate = historical_median + estimated_price_fallback_margin_eur_per_mwh
        export estimate = historical_median - estimated_price_fallback_margin_eur_per_mwh
    """
    margin = cfg.economics.estimated_price_fallback_margin_eur_per_mwh
    return median_eur_mwh + margin, median_eur_mwh - margin


def find_most_recent_complete_day(
    before: date, prices_dir: str = paths.PRICES_DIR, max_lookback_days: int = 14
) -> Optional[date]:
    """Najde nejnovější den PŘED `before`, který má kompletní actual cenová
    data. Pomocná funkce pro budoucí "tvar dne" heuristiky (analogicky k v8),
    NENÍ součástí 6.3 kvantilového modelu - jen stavební kámen."""
    d = before - timedelta(days=1)
    for _ in range(max_lookback_days):
        if load_actual_day_prices(d, prices_dir) is not None:
            return d
        d -= timedelta(days=1)
    return None
