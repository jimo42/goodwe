"""
Načítání předpovědi počasí a PV výroby (beze zápisu) pro planner v10.

Autoritativní zdroj pravidel: ARCHITECTURE_DESIGN_v10_FINAL.md sekce 6.3
("předpovězená PV výroba a dostupné počasí" jako vstup do cenového
estimatoru) a sekce 15.1 (`pv_estimate_kwh` v `forecast_48h.json`).

Zdrojová data na serveru (ověřeno ssh průzkumem, viz lib/paths.py docstring):
    weather-forecast/weather/{YYYY-MM-DD}.csv
        "datetime;sun_pct;cloudcover_pct;shortwave_wm2;pv_estimate_kwh"
        HODINOVĚ (jeden řádek na hodinu, ~24-25 řádků/den vč. headeru),
        generováno weather-forecast/download-weather.py (Open-Meteo +
        astral), přepisováno každou hodinu (`55 * * * *` v crontabu) s
        horizontem 48 hodin dopředu.

Rozsah tohoto modulu (MUST):
  - Čtení hodinových záznamů pro daný den.
  - Rozdělení hodinového `pv_estimate_kwh` na 15min plánovací sloty
    (planning_step_minutes z config.toml) - APROXIMACE rovnoměrným dělením,
    protože zdrojová data mají jen hodinové rozlišení (v souladu s
    poznámkou v předchozí v8 implementaci `remote_staging/planner/lib/
    weather.py::pv_estimate_kwh_for_slot` - vědomé zjednodušení pro v1, ne
    definitivní fyzikální model).

Žádná funkce v tomto modulu nic nezapisuje do souborů ani zařízení.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional

from . import paths

WeatherRecord = dict[str, float]


def _parse_weather_csv_text(text: str) -> dict[str, WeatherRecord]:
    """Parsuje obsah hodinového CSV -> {'YYYY-MM-DDTHH:MM': {...}}.

    Neplatné/nerozpoznané řádky (chybějící pole, chybný počet kolonek,
    nečíselná hodnota) se tiše přeskočí - robustní čtení, žádná tvrdá
    výjimka za běhu stateless cron procesu."""
    out: dict[str, WeatherRecord] = {}
    header: Optional[list[str]] = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(";")
        if header is None:
            header = parts
            continue
        if len(parts) != len(header):
            continue
        row = dict(zip(header, parts))
        ts = row.get("datetime")
        if not ts:
            continue
        try:
            out[ts] = {
                "sun_pct": float(row.get("sun_pct", 0) or 0),
                "cloudcover_pct": float(row.get("cloudcover_pct", 0) or 0),
                "shortwave_wm2": float(row.get("shortwave_wm2", 0) or 0),
                "pv_estimate_kwh": float(row.get("pv_estimate_kwh", 0) or 0),
            }
        except ValueError:
            continue
    return out


def read_weather_csv(path: str) -> dict[str, WeatherRecord]:
    """Přečte jeden CSV soubor počasí ze zadané cesty. Prázdný dict, pokud
    soubor neexistuje nebo je nečitelný."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return _parse_weather_csv_text(f.read())
    except OSError:
        return {}


def load_day_weather(
    d: date, weather_dir: str = paths.WEATHER_DIR
) -> dict[str, WeatherRecord]:
    """Načte hodinovou předpověď pro daný den -> {'YYYY-MM-DDTHH:MM': {...}}.
    Prázdný dict, pokud den nemá data (žádná výjimka)."""
    path = os.path.join(weather_dir, f"{d.strftime('%Y-%m-%d')}.csv")
    return read_weather_csv(path)


def get_weather_for_hour(
    dt: datetime, weather_dir: str = paths.WEATHER_DIR
) -> Optional[WeatherRecord]:
    """Vrátí hodinový záznam počasí pro daný čas (zaokrouhleno dolů na
    hodinu), nebo None, pokud pro danou hodinu není záznam."""
    day_data = load_day_weather(dt.date(), weather_dir)
    key = dt.strftime("%Y-%m-%dT%H:00")
    return day_data.get(key)


def pv_estimate_kwh_for_slot(
    dt: datetime, slot_minutes: int, weather_dir: str = paths.WEATHER_DIR
) -> float:
    """Rovnoměrně rozdělí hodinový odhad `pv_estimate_kwh` na plánovací
    sloty o délce `slot_minutes` (typicky 15, dle config.toml
    `system.planning_step_minutes`). APROXIMACE - viz docstring modulu.
    Vrátí 0.0, pokud pro daný čas není záznam o počasí (konzervativní
    výchozí hodnota, ŽÁDNÁ PV výroba)."""
    weather = get_weather_for_hour(dt, weather_dir)
    if not weather:
        return 0.0
    fraction = slot_minutes / 60.0
    return weather["pv_estimate_kwh"] * fraction


def sun_pct_for_slot(
    dt: datetime, weather_dir: str = paths.WEATHER_DIR
) -> Optional[float]:
    """Vrátí `sun_pct` (0-100) pro hodinu odpovídající danému slotu, nebo
    None, pokud záznam chybí. Užitečné pro diagnostiku/reasoning (evidence),
    NENÍ přímo použito v ekonomických vzorcích."""
    weather = get_weather_for_hour(dt, weather_dir)
    if not weather:
        return None
    return weather["sun_pct"]


def cloudcover_pct_for_slot(
    dt: datetime, weather_dir: str = paths.WEATHER_DIR
) -> Optional[float]:
    """Vrátí `cloudcover_pct` (0-100) pro hodinu odpovídající danému slotu,
    nebo None, pokud záznam chybí."""
    weather = get_weather_for_hour(dt, weather_dir)
    if not weather:
        return None
    return weather["cloudcover_pct"]
