#!/usr/bin/env python3
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

TOKEN_FILE = "/home/automatization/goodwe/conf/entsoe-token"
EIC_CZ = "10YCZ-CEPS-----N"
EXPECTED_SLOTS = 96


def _text(element: Optional[ET.Element]) -> str:
    return "" if element is None or element.text is None else element.text.strip()


def _slot_label(position: int) -> str:
    minutes = (position - 1) * 15
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _format_price(value: str) -> str:
    return f"{float(value.replace(',', '.')):.2f}".replace(".", ",")


def _parse_prices(xml_bytes: bytes) -> dict[int, str]:
    root = ET.fromstring(xml_bytes)
    root_name = root.tag.split("}", 1)[-1]
    if root_name == "Acknowledgement_MarketDocument":
        reasons = []
        for reason in root.findall(".//{*}Reason"):
            code = _text(reason.find("{*}code"))
            text = _text(reason.find("{*}text"))
            reasons.append(f"{code} {text}".strip())
        reason_msg = "; ".join(reasons) if reasons else "without reason text"
        raise ValueError(f"ENTSO-E returned acknowledgement instead of prices: {reason_msg}")

    candidates: list[dict[int, str]] = []
    for period in root.findall(".//{*}Period"):
        resolution = _text(period.find("{*}resolution"))
        prices: dict[int, str] = {}
        for point in period.findall("{*}Point"):
            position_text = _text(point.find("{*}position"))
            price_text = _text(point.find("{*}price.amount"))
            if not position_text or not price_text:
                continue
            prices[int(position_text)] = _format_price(price_text)
        if prices:
            candidates.append(prices)
            if resolution == "PT15M" and len(prices) >= EXPECTED_SLOTS:
                return prices

    if not candidates:
        raise ValueError("ENTSO-E response contains no price points")
    best = max(candidates, key=len)
    return best


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: getPricesFromENTSOE.py YYYY-MM-DD", file=sys.stderr)
        return 1

    try:
        token = Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
    except Exception as exc:
        print(f"Error while reading token from {TOKEN_FILE}: {exc}", file=sys.stderr)
        return 1

    try:
        local_date = datetime.strptime(argv[1], "%Y-%m-%d")
    except ValueError:
        print("Wrong date, use format YYYY-MM-DD", file=sys.stderr)
        return 1

    start_utc = (local_date - timedelta(hours=2)).strftime("%Y%m%d%H%M")
    end_utc = (local_date + timedelta(days=1) - timedelta(hours=2)).strftime("%Y%m%d%H%M")
    params = {
        "securityToken": token,
        "documentType": "A44",
        "in_Domain": EIC_CZ,
        "out_Domain": EIC_CZ,
        "periodStart": start_utc,
        "periodEnd": end_utc,
    }

    try:
        response = requests.get("https://web-api.tp.entsoe.eu/api", params=params, timeout=30)
        response.raise_for_status()
        prices = _parse_prices(response.content)
    except Exception as exc:
        print(f"Error while fetching ENTSO-E prices for {argv[1]}: {exc}", file=sys.stderr)
        return 2

    missing = [pos for pos in range(1, EXPECTED_SLOTS + 1) if pos not in prices]
    if missing:
        print(f"ENTSO-E price data incomplete, missing positions: {missing[:10]}", file=sys.stderr)
        return 2

    lines = [f"{_slot_label(pos)};{prices[pos]}\n" for pos in range(1, EXPECTED_SLOTS + 1)]
    out_path = Path(f"{local_date.date()}_check.csv")
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text("".join(lines), encoding="utf-8")
    tmp_path.replace(out_path)
    print(f"Done: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
