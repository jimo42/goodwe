#!/usr/bin/env python3
"""Download and store a three-day hourly weather/PV forecast.

VERSION = "1.1"

Changelog:
- v1.1 (2026-08-14): Store all 72 requested forecast hours so a 48-hour
  planner horizon remains covered immediately after midnight.
- v1.0: Forecast sunshine and PV output using Open-Meteo and astral.
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
from astral.sun import azimuth, elevation


VERSION = "1.1"
FORECAST_DAYS = 3
FORECAST_HOURS = FORECAST_DAYS * 24  # Nominal count; DST days can contain 23/25 hours.
TIMEZONE = "Europe/Prague"
CONF_PATH = Path(__file__).resolve().parent.parent / "conf" / "weather.conf"
OUTDIR = Path(__file__).resolve().parent / "weather"
CLOUD_TO_SUN_BANDS = [(20, 100), (50, 60), (80, 30), (101, 10)]


def load_config(path: Path = CONF_PATH) -> dict[str, float]:
    if not path.exists():
        raise FileNotFoundError(f"Missing config: {path}")
    out: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, val = line.split("=", 1)
            out[key.strip()] = float(val.strip())
    return out


def map_cloud_to_sunpct(cloud: int) -> int:
    for upper, sunpct in CLOUD_TO_SUN_BANDS:
        if cloud < upper:
            return sunpct
    return 10


def fetch_open_meteo(lat: float, lon: float) -> dict:
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&hourly=cloudcover,shortwave_radiation"
        f"&forecast_days={FORECAST_DAYS}&timezone={TIMEZONE}"
    )
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.json()


def solar_angles(ts: str, observer) -> tuple[float, float]:
    dt = datetime.fromisoformat(ts).replace(tzinfo=ZoneInfo(TIMEZONE))
    elev = elevation(observer, dt)
    return max(90 - elev, 0.0), azimuth(observer, dt)


def solar_projection(zenith_deg: float, azimuth_deg: float, tilt: float, panel_azimuth: float) -> float:
    zenith_rad = math.radians(zenith_deg)
    azimuth_rad = math.radians(azimuth_deg)
    tilt_rad = math.radians(tilt)
    panel_az_rad = math.radians(panel_azimuth)
    cos_theta = (
        math.cos(zenith_rad) * math.cos(tilt_rad)
        + math.sin(zenith_rad) * math.sin(tilt_rad) * math.cos(azimuth_rad - panel_az_rad)
    )
    return max(cos_theta, 0.0)


def split_by_day(timestamps: list[str], **columns) -> dict[str, dict[str, list]]:
    out: dict[str, dict[str, list]] = {}
    for idx, ts in enumerate(timestamps):
        key = ts[:10]
        per_day = out.setdefault(key, {"ts": [], **{name: [] for name in columns}})
        per_day["ts"].append(ts)
        for name, values in columns.items():
            per_day[name].append(values[idx])
    return out


def save_csv(day: str, rows: list[tuple], outdir: Path = OUTDIR) -> None:
    outdir.mkdir(exist_ok=True)
    path = outdir / f"{day}.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["datetime", "sun_pct", "cloudcover_pct", "shortwave_wm2", "pv_estimate_kwh"])
        writer.writerows(rows)
    print(f"[weather] wrote {path}")


def main() -> int:
    print(f"download-weather.py v{VERSION}")
    try:
        config = load_config()
        data = fetch_open_meteo(config["LAT"], config["LON"])
        hourly = data["hourly"]
        timestamps = list(hourly["time"])
        clouds = [int(value) for value in hourly["cloudcover"]]
        radiation = [float(value) for value in hourly["shortwave_radiation"]]
        forecast_dates = {timestamp[:10] for timestamp in timestamps}
        if not (len(timestamps) == len(clouds) == len(radiation)):
            raise ValueError("Open-Meteo hourly arrays have different lengths")
        if len(forecast_dates) != FORECAST_DAYS:
            raise ValueError(
                f"Open-Meteo returned incomplete day coverage: {len(forecast_dates)}/{FORECAST_DAYS} days"
            )
    except Exception as exc:  # Network/data failure must preserve existing CSV files.
        print(f"ERROR: cannot fetch complete weather data: {exc}", file=sys.stderr)
        print("[weather] skipping CSV write due to download error")
        return 1

    tilt = config.get("PANEL_TILT", 20.0)
    panel_azimuth = config.get("PANEL_AZIMUTH", 180.0)
    peak_kw = config.get("PV_PEAK_KW", 10.0)
    observer = LocationInfo("MySite", "CZ", TIMEZONE, config["LAT"], config["LON"]).observer
    per_day = split_by_day(timestamps, cloud=clouds, sw=radiation)
    for day, columns in per_day.items():
        rows = []
        for ts, cloud, shortwave in zip(columns["ts"], columns["cloud"], columns["sw"]):
            zenith, sun_azimuth = solar_angles(ts, observer)
            projection = solar_projection(zenith, sun_azimuth, tilt, panel_azimuth)
            estimated_kwh = round(peak_kw * projection * (shortwave / 1000.0), 3)
            rows.append((ts, map_cloud_to_sunpct(cloud), cloud, shortwave, estimated_kwh))
        save_csv(day, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())