#!/usr/bin/env python3
"""
Forecast sunshine & PV output using astral 3.2 (Debian-compatible).
- Uses Open-Meteo API for cloudcover & radiation
- Uses astral.sun.elevation & azimuth with LocationInfo/observer
- Estimates PV output (kWh) per hour for next 48 hours


Example of conf file:
# Weather configuration for sun exposure calculation
# All angles in degrees

# Location of the panels (Czechia, Chomutov)
LAT=50.460
LON=13.417

# Panel orientation and geometry
# Panel azimuth (0 = North, 90 = East, 180 = South, 270 = West)
PANEL_AZIMUTH=200
# Panel tilt from horizontal (0 = flat, 90 = vertical)
PANEL_TILT=30

# System size - total peak power of FVE (in kW)
PV_PEAK_KW=10

(End of conf file example)


"""

from __future__ import annotations
import csv
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from astral import LocationInfo
from astral.sun import elevation, azimuth

# --- Load config ---
CONF_PATH = Path(__file__).resolve().parent.parent / "conf" / "weather.conf"
if not CONF_PATH.exists():
    print(f"Missing config: {CONF_PATH}", file=sys.stderr)
    sys.exit(1)

CONF = {}
for line in CONF_PATH.read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        key, val = line.split("=", 1)
        CONF[key.strip()] = float(val.strip())

LAT = CONF.get("LAT")
LON = CONF.get("LON")
TILT = CONF.get("PANEL_TILT", 20)
AZIMUTH = CONF.get("PANEL_AZIMUTH", 180)
PV_PEAK_KW = CONF.get("PV_PEAK_KW", 10)

TIMEZONE = "Europe/Prague"
FORECAST_HOURS = 48
OUTDIR = Path(__file__).resolve().parent / "weather"
OUTDIR.mkdir(exist_ok=True)

CLOUD_TO_SUN_BANDS = [(20, 100), (50, 60), (80, 30), (101, 10)]

def map_cloud_to_sunpct(cloud: int) -> int:
    for upper, sunpct in CLOUD_TO_SUN_BANDS:
        if cloud < upper:
            return sunpct
    return 10

def fetch_open_meteo() -> dict:
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
        "&hourly=cloudcover,shortwave_radiation"
        f"&forecast_days=3&timezone={TIMEZONE}"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"ERROR: cannot fetch weather data: {e}", file=sys.stderr)
        sys.exit(1)

def solar_angles(ts: str, observer) -> tuple[float, float]:
    dt = datetime.fromisoformat(ts).replace(tzinfo=ZoneInfo(TIMEZONE))
    elev = elevation(observer, dt)
    az = azimuth(observer, dt)
    zen = max(90 - elev, 0.0)
    return zen, az

def solar_projection(zenith_deg: float, azimuth_deg: float) -> float:
    zenith_rad = math.radians(zenith_deg)
    azimuth_rad = math.radians(azimuth_deg)
    tilt_rad = math.radians(TILT)
    panel_az_rad = math.radians(AZIMUTH)
    cos_theta = (
        math.cos(zenith_rad) * math.cos(tilt_rad) +
        math.sin(zenith_rad) * math.sin(tilt_rad) * math.cos(azimuth_rad - panel_az_rad)
    )
    return max(cos_theta, 0)

def split_by_day(timestamps: list[str], **cols) -> dict[str, dict[str, list]]:
    out: dict[str, dict[str, list]] = {}
    for idx, ts in enumerate(timestamps):
        key = ts[:10]
        per_day = out.setdefault(key, {"ts": [], **{k: [] for k in cols}})
        per_day["ts"].append(ts)
        for k, arr in cols.items():
            per_day[k].append(arr[idx])
    return out

def save_csv(day: str, rows: list[tuple]) -> None:
    path = OUTDIR / f"{day}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["datetime", "sun_pct", "cloudcover_pct", "shortwave_wm2", "pv_estimate_kwh"])
        w.writerows(rows)
    print(f"[weather] wrote {path}")

def main():
    
    try:
        j = fetch_open_meteo()
    except SystemExit:
        print('[weather] skipping CSV write due to download error')
        return
    h = j["hourly"]
    timestamps = h["time"][:FORECAST_HOURS]
    clouds = [int(x) for x in h["cloudcover"][:FORECAST_HOURS]]
    sw = [float(x) for x in h["shortwave_radiation"][:FORECAST_HOURS]]

    per_day = split_by_day(timestamps, cloud=clouds, sw=sw)

    loc = LocationInfo("MySite", "CZ", TIMEZONE, LAT, LON)
    obs = loc.observer

    for day, cols in per_day.items():
        rows = []
        for ts, cloud, srad in zip(cols["ts"], cols["cloud"], cols["sw"]):
            sun_pct = map_cloud_to_sunpct(cloud)
            zenith, az = solar_angles(ts, obs)
            projection = solar_projection(zenith, az)
            est_kw = PV_PEAK_KW * projection * (srad / 1000)
            est_kwh = round(est_kw, 3)
            rows.append((ts, sun_pct, cloud, srad, est_kwh))
        save_csv(day, rows)

if __name__ == "__main__":
    main()

