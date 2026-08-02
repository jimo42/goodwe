"""
Centrální cesty k datovým adresářům na serveru pro planner v10.

Tyto cesty jsou čistě operační (kde na disku hledat vstupní CSV soubory) a
NEJSOU součástí konfiguračního schématu v `config.toml`/`lib/config.py` (to
je vyhrazeno pro obchodní/rozhodovací parametry, viz docstring
`lib/config.py`). Změna adresářové struktury na serveru je provozní změna
kódu, ne konfigurace - proto samostatný modul mimo `Config` dataclass.

Ověřeno přímým ssh průzkumem serveru (2026-07-21/22):
    /home/automatization/goodwe/energy-prices/{YYYY-MM-DD}.csv
        formát 'HH:MM;cena_s_čárkou' (EUR/MWh), 96 řádků/den při 15min kroku.
    /home/automatization/goodwe/energy-prices/{YYYY-MM-DD}_check.csv
        druhý zdroj (ENTSOE) - dle `download-prices.sh` zůstává na disku jen
        pokud se s primárním souborem neshodují, nebo dokud primární soubor
        ještě nebyl stažen; jinak se po shodě maže.
    /home/automatization/goodwe/weather-forecast/weather/{YYYY-MM-DD}.csv
        formát 'datetime;sun_pct;cloudcover_pct;shortwave_wm2;pv_estimate_kwh',
        HODINOVĚ (~24 datových řádků + header), generováno
        weather-forecast/download-weather.py (Open-Meteo + astral).
"""
from __future__ import annotations

import os

BASE_DIR = "/home/automatization/goodwe"

PRICES_DIR = os.path.join(BASE_DIR, "energy-prices")
WEATHER_DIR = os.path.join(BASE_DIR, "weather-forecast", "weather")
GOODWE_REPORTS_DIR = os.path.join(BASE_DIR, "logs", "goodwe-reports")
