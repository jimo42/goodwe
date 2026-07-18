#!/usr/bin/env python3
"""
Analyza logu rele bojleru (relay.log) - korelace se spotrebou domu ze
stridace (goodwe_stats_*), abychom zjistili:
1. Skutecny narust vykonu pri zapnuti kazde faze (1/2/3) - overeni ze
   kazda faze je fakt ~2 kW.
2. Jak casto zustava rele sepnute, ale termostat uz bojler odpojil
   (tj. house_consumption neodpovida ocekavanemu naruzstu) - to potvrdi/
   vyvrati hypotezu uzivatele, ze "rele ON" != "fakt se topi".
3. Odhad DOBY, po kterou se realne topi po sepnuti (kdy skutecne klesne
   spotreba na baseline, i kdyz rele zustava ON) - pro zpresneni
   BOILER_FULL_HEAT_TIME_HOURS.

Cte VSECHNA data lokalne, ven jde jen agregovany JSON (viz analyze_history.py
pro stejnou zasadu).
"""
from __future__ import annotations

import glob
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta

BASE = "/home/automatization/goodwe"
RELAY_LOG = os.path.join(BASE, "logs", "relay.log")
REPORTS_DIR = os.path.join(BASE, "logs", "goodwe-reports")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

NUM_RE = re.compile(r"=\s*(-?[\d.]+)")


def parse_stats_content(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if ":" not in line or "=" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        m = NUM_RE.search(line)
        if m:
            try:
                out[key] = float(m.group(1))
            except ValueError:
                pass
    return out


def load_relay_transitions():
    """Parsuje relay.log radky typu:
    2026-07-18_18:00 prev: 1110, cmd: C ON,  new: 1111
    Vraci list (timestamp, cmd_str, prev_bits, new_bits).
    Bity: poradi je "1 2 3 C" -> pozice 0=faze1,1=faze2,2=faze3,3=cerpadlo
    (dle pozorovani vzorku dat, ne 100% jiste dokumentovano ve skriptu)."""
    transitions = []
    with open(RELAY_LOG, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            m = re.match(
                r"(\d{4}-\d{2}-\d{2})_(\d{2}:\d{2}) prev: (\d+), cmd: (.+?), new: (\d+)",
                line,
            )
            if not m:
                continue
            date_s, time_s, prev_bits, cmd, new_bits = m.groups()
            try:
                ts = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            transitions.append((ts, cmd.strip(), prev_bits, new_bits))
    return transitions


def build_stats_index():
    """Jednorazove naskenuje adresar goodwe-reports (pres os.scandir, ne glob
    s shell-style expanzi, aby to bylo O(n) a nespadlo na 'argument list too
    long') a vrati dict "YYYYmmdd_HHMM" -> plna cesta k souboru (prvni nalezeny
    v danou minutu, sekundy ignorujeme pro ucely tohoto parovani)."""
    index = {}
    print("Indexuji adresar goodwe-reports (jednorazove)...", file=sys.stderr)
    n = 0
    with os.scandir(REPORTS_DIR) as it:
        for entry in it:
            if not entry.is_file():
                continue
            m = re.match(r"goodwe_stats_(\d{8}_\d{4})\d{2}$", entry.name)
            if not m:
                continue
            key = m.group(1)
            # pri kolizi (vic souboru ve stejne minute) nech??me prvni nalezeny
            index.setdefault(key, entry.path)
            n += 1
    print(f"  -> naindexovano {n} souboru, {len(index)} unikatnich minut", file=sys.stderr)
    return index


def find_stats_file_near(index: dict, ts: datetime, tolerance_minutes=2):
    """Najde nejblizsi goodwe_stats_* soubor k danemu casu (+- tolerance) - O(1)
    lookup v predpocitanem indexu, misto glob.glob() volaneho pro kazdy dotaz."""
    for delta in range(0, tolerance_minutes + 1):
        for sign in (0, 1, -1):
            if sign == 0 and delta != 0:
                continue
            candidate_ts = ts + timedelta(minutes=sign * delta)
            key = candidate_ts.strftime("%Y%m%d_%H%M")
            path = index.get(key)
            if path:
                return path
    return None


def read_house_consumption(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = parse_stats_content(f.read())
        return data.get("house_consumption")
    except OSError:
        return None



def main():
    stats_index = build_stats_index()

    print("Nacitam relay.log prechody...", file=sys.stderr)
    transitions = load_relay_transitions()
    print(f"  -> {len(transitions)} prechodu", file=sys.stderr)

    # Zajimaji nas prechody OFF->ON jednotlivych fazi (cmd obsahuje "1 ON"/"2 ON"/"3 ON")
    # a naopak ON->OFF, abychom zmerili skok ve spotrebe.
    phase_on_deltas = {"1": [], "2": [], "3": []}
    phase_off_deltas = {"1": [], "2": [], "3": []}

    # Pro analyzu "rele ON ale netopi se" potrebujeme pro kazdy interval, kdy
    # byla nejaka faze ON, vzorkovat spotrebu v prubehu (ne jen na zacatku/konci)
    matched = 0
    unmatched = 0

    for i, (ts, cmd, prev_bits, new_bits) in enumerate(transitions):
        m = re.match(r"^(\d) (ON|OFF)\s*$", cmd)
        if not m:
            continue  # napr. "C ON"/"C OFF" = cerpadlo, nebo neshoda formatu
        phase, action = m.groups()
        if phase not in phase_on_deltas:
            continue

        before_path = find_stats_file_near(stats_index, ts - timedelta(minutes=1))
        after_path = find_stats_file_near(stats_index, ts + timedelta(minutes=1))

        if not before_path or not after_path:
            unmatched += 1
            continue
        before = read_house_consumption(before_path)
        after = read_house_consumption(after_path)
        if before is None or after is None:
            unmatched += 1
            continue
        matched += 1
        delta = after - before
        if action == "ON":
            phase_on_deltas[phase].append(delta)
        else:
            phase_off_deltas[phase].append(-delta)  # kladna hodnota = pokles spotreby

    def summarize(deltas):
        if len(deltas) < 3:
            return {"n": len(deltas), "note": "malo vzorku"}
        s = sorted(deltas)
        return {
            "n": len(s),
            "median_w": round(statistics.median(s), 1),
            "p25_w": round(s[len(s)//4], 1),
            "p75_w": round(s[3*len(s)//4], 1),
            "min_w": round(min(s), 1),
            "max_w": round(max(s), 1),
        }

    result = {
        "generated_at": datetime.now().isoformat(),
        "n_transitions_total": len(transitions),
        "n_phase_transitions_matched_with_stats": matched,
        "n_phase_transitions_unmatched": unmatched,
        "power_jump_on_phase_activation_w": {
            phase: summarize(vals) for phase, vals in phase_on_deltas.items()
        },
        "power_drop_on_phase_deactivation_w": {
            phase: summarize(vals) for phase, vals in phase_off_deltas.items()
        },
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = os.path.join(
        RESULTS_DIR, f"boiler_relay_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Hotovo: {out_path}", file=sys.stderr)
    print(out_path)


if __name__ == "__main__":
    main()

