#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


OTE_URL = "https://www.ote-cr.cz/pw-data/chart-data/01"
EXPECTED_SLOTS = 96


def _format_price(value: Any) -> str:
    price = float(value)
    return f"{price:.2f}".replace(".", ",")


def _slot_label(position: int) -> str:
    minutes = (position - 1) * 15
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _select_price_line(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data_lines = payload.get("data", {}).get("dataLine", [])
    if not isinstance(data_lines, list):
        raise ValueError("OTE JSON does not contain data.dataLine list")

    candidates = []
    for line in data_lines:
        if not isinstance(line, dict):
            continue
        title = str(line.get("title") or "")
        tooltip = str(line.get("tooltip") or "")
        use_y2 = bool(line.get("useY2"))
        points = line.get("point")
        if not isinstance(points, list):
            continue
        label = f"{title} {tooltip}".lower()
        if "15min" in label and "cena" in label and not use_y2:
            candidates.append(points)

    if not candidates:
        raise ValueError("OTE JSON does not contain a 15min price line")
    if len(candidates) > 1:
        # Keep deterministic behaviour; all current OTE payloads contain exactly one.
        return candidates[0]
    return candidates[0]


def fetch_ote_prices(day: str) -> list[str]:
    datetime.strptime(day, "%Y-%m-%d")
    response = requests.get(
        OTE_URL,
        params={"report_date": day, "time_resolution": "PT15M", "language": "cs"},
        timeout=30,
        headers={"User-Agent": "goodwe-price-fetch/1.0"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("OTE response is not a JSON object")

    points = _select_price_line(payload)
    by_position: dict[int, str] = {}
    for point in points:
        if not isinstance(point, dict):
            continue
        position = int(point["x"])
        by_position[position] = _format_price(point["y"])

    missing = [pos for pos in range(1, EXPECTED_SLOTS + 1) if pos not in by_position]
    if missing:
        raise ValueError(f"OTE price line is incomplete, missing positions: {missing[:10]}")

    return [f"{_slot_label(pos)};{by_position[pos]}\n" for pos in range(1, EXPECTED_SLOTS + 1)]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: getPricesFromOTE.py YYYY-MM-DD", file=sys.stderr)
        return 1
    day = argv[1]
    try:
        lines = fetch_ote_prices(day)
    except Exception as exc:
        print(f"Error while fetching OTE prices for {day}: {exc}", file=sys.stderr)
        return 2

    out_path = Path(f"{day}.csv")
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text("".join(lines), encoding="utf-8")
    tmp_path.replace(out_path)
    print(f"Done: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
