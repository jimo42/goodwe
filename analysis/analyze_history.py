#!/usr/bin/env python3
"""
Analyza historickych dat pro ladeni parametru rizeni FVE.

DULEZITE: Tento skript bezi PRIMO NA SERVERU a zpracovava VSECHNA surova data
lokalne. Ven (do vysledneho JSON souboru v ./results/) jdou POUZE agregovane
statistiky (percentily, prumery, distribuce) - NIKDY jednotlive syrove zaznamy.
Vysledny JSON je male velikosti a je urcen k odeslani/precteni mimo server.

Zdroje dat:
  - /home/automatization/goodwe/logs/goodwe-reports/goodwe_stats_* (aktualni, nezabalene)
  - /home/automatization/goodwe/logs/goodwe-reports/reports_YYYYMM.tgz (archiv, cteno
    primo z tar bez rozbalovani na disk)
  - /home/automatization/goodwe/energy-prices/*.csv (ceny, 15min sloty)
  - /home/automatization/goodwe/weather-forecast/weather/*.csv (pocasi/PV odhad)

Pouziti:
  python3 analyze_history.py [--months-sample N] [--all-archives]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
import tarfile
from collections import defaultdict
from datetime import datetime

BASE = "/home/automatization/goodwe"
REPORTS_DIR = os.path.join(BASE, "logs", "goodwe-reports")
PRICES_DIR = os.path.join(BASE, "energy-prices")
WEATHER_DIR = os.path.join(BASE, "weather-forecast", "weather")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

NUM_RE = re.compile(r"=\s*(-?[\d.]+)")


def parse_stats_content(text: str) -> dict:
    """Parsuje obsah jednoho goodwe_stats_* souboru na dict klic->hodnota (float/str)."""
    out = {}
    for line in text.splitlines():
        if ":" not in line or "=" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        rhs = line.split("=", 1)[1].strip()
        if key == "timestamp":
            out["timestamp"] = rhs
            continue
        m = NUM_RE.search(line)
        if m:
            try:
                out[key] = float(m.group(1))
            except ValueError:
                pass
    return out


def iter_current_files():
    """Generator - jmeno souboru + jeho obsah, pro aktualni (nezabalene) stats soubory."""
    pattern = os.path.join(REPORTS_DIR, "goodwe_stats_*")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                yield os.path.basename(path), f.read()
        except OSError:
            continue


def iter_archive_files(tgz_path: str):
    """Generator - jmeno souboru + obsah, cteno primo z tar archivu (bez rozbaleni na disk)."""
    try:
        with tarfile.open(tgz_path, "r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                try:
                    content = f.read().decode("utf-8", errors="ignore")
                except Exception:
                    continue
                yield os.path.basename(member.name), content
    except (tarfile.TarError, OSError) as e:
        print(f"WARN: nelze cist archiv {tgz_path}: {e}", file=sys.stderr)


def parse_ts_from_filename(name: str):
    """goodwe_stats_20260718_014101 -> datetime"""
    m = re.search(r"goodwe_stats_(\d{8})_(\d{6})", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


# --- Agregacni struktury ------------------------------------------------

night_load_by_month = defaultdict(list)   # month -> [house_consumption W] pro noc (00-05h) a nizky PV
pv_start_hour_by_month = defaultdict(list)  # month -> [hodina prvniho PV > 100W v danem dni]
soc_samples = 0
records_processed = 0
day_first_pv = {}  # "YYYY-MM-DD" -> nejdrivejsi hodina/minuta s PV > 100W (docasne behem zpracovani)
day_last_pv = {}   # "YYYY-MM-DD" -> nejpozdejsi cas s PV > 100W (pro odhad zapadu/produkcni okno)
# Pozn.: soubory goodwe_stats_* NEOBSAHUJI kumulativni "dnesni vyroba" (e_day) -
# skutecnou dennodni PV energii proto odhadujeme integraci vykonu ppv1+ppv2 pres
# vzorky (predpoklad ~1 vzorek/min dle cronu), viz day_pv_energy_wh.
day_pv_energy_wh = defaultdict(float)  # "YYYY-MM-DD" -> odhad Wh (soucet ppv_total/60)
day_pv_sample_count = defaultdict(int)  # pro kontrolu hustoty vzorkovani



def process_record(name: str, content: str):
    global records_processed
    ts = parse_ts_from_filename(name)
    if ts is None:
        return
    data = parse_stats_content(content)
    if not data:
        return
    records_processed += 1
    month_key = ts.strftime("%Y-%m")
    day_key = ts.strftime("%Y-%m-%d")

    ppv1 = data.get("ppv1", 0.0)
    ppv2 = data.get("ppv2", 0.0)
    ppv_total = ppv1 + ppv2
    house = data.get("house_consumption")

    # Nocni baseline: 00:00-05:00 a soucasne temer nulova PV vyroba (aby to nebyl
    # den se sviticim mesicem :) ale realna nocni spotreba)
    if house is not None and 0 <= ts.hour <= 5 and ppv_total < 20:
        night_load_by_month[month_key].append(house)

    # Sledovani prvniho/posledniho vyskytu PV > 100 W v danem dni (pro odhad
    # delky "produkcniho okna" slunce)
    if ppv_total > 100:
        minute_of_day = ts.hour * 60 + ts.minute
        if day_key not in day_first_pv or minute_of_day < day_first_pv[day_key]:
            day_first_pv[day_key] = minute_of_day
        if day_key not in day_last_pv or minute_of_day > day_last_pv[day_key]:
            day_last_pv[day_key] = minute_of_day

    # Odhad denni PV energie integraci vykonu (predpoklad ~1 vzorek/min)
    day_pv_energy_wh[day_key] += ppv_total / 60.0
    day_pv_sample_count[day_key] += 1



def summarize_night_load():
    out = {}
    for month, values in sorted(night_load_by_month.items()):
        if len(values) < 10:
            continue
        values_sorted = sorted(values)
        out[month] = {
            "n": len(values),
            "median_w": round(statistics.median(values_sorted), 1),
            "p25_w": round(values_sorted[len(values_sorted)//4], 1),
            "p75_w": round(values_sorted[3*len(values_sorted)//4], 1),
            "min_w": round(min(values_sorted), 1),
            "max_w": round(max(values_sorted), 1),
        }
    return out


def summarize_pv_window():
    """Z day_first_pv/day_last_pv spocita distribuci hodiny prvni/posledni PV produkce
    po mesicich (pro dynamickou nocni rezervu - kdy priblizne vychazi/zapada produkce)."""
    by_month_first = defaultdict(list)
    by_month_last = defaultdict(list)
    for day_key, minute in day_first_pv.items():
        month = day_key[:7]
        by_month_first[month].append(minute)
    for day_key, minute in day_last_pv.items():
        month = day_key[:7]
        by_month_last[month].append(minute)

    out = {}
    for month in sorted(set(list(by_month_first.keys()) + list(by_month_last.keys()))):
        firsts = sorted(by_month_first.get(month, []))
        lasts = sorted(by_month_last.get(month, []))
        entry = {}
        if firsts:
            med = statistics.median(firsts)
            entry["median_pv_start"] = f"{int(med)//60:02d}:{int(med)%60:02d}"
            entry["n_days_first"] = len(firsts)
        if lasts:
            med = statistics.median(lasts)
            entry["median_pv_end"] = f"{int(med)//60:02d}:{int(med)%60:02d}"
            entry["n_days_last"] = len(lasts)
        if entry:
            out[month] = entry
    return out


# --- Ceny -----------------------------------------------------------------

def analyze_prices():
    files = sorted(glob.glob(os.path.join(PRICES_DIR, "20*.csv")))
    files = [f for f in files if "_check" not in f]
    all_prices = []
    negative_count = 0
    extreme_count = 0  # >=250
    by_month_avg = defaultdict(list)
    n_days = 0
    for path in files:
        day = os.path.basename(path)[:10]
        month = day[:7]
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            continue
        day_prices = []
        for line in lines:
            line = line.strip()
            if not line or ";" not in line:
                continue
            parts = line.split(";")
            if len(parts) != 2:
                continue
            price_str = parts[1].replace(",", ".").strip()
            try:
                price = float(price_str)
            except ValueError:
                continue
            day_prices.append(price)
        if len(day_prices) < 90:  # neuplny den, preskocit
            continue
        n_days += 1
        all_prices.extend(day_prices)
        by_month_avg[month].extend(day_prices)
        negative_count += sum(1 for p in day_prices if p < 0)
        extreme_count += sum(1 for p in day_prices if p >= 250)

    if not all_prices:
        return {"error": "zadna cenova data nenalezena"}

    all_sorted = sorted(all_prices)
    n = len(all_sorted)
    result = {
        "n_days_analyzed": n_days,
        "n_slots_total": n,
        "mean_eur_mwh": round(statistics.mean(all_sorted), 2),
        "median_eur_mwh": round(statistics.median(all_sorted), 2),
        "p10_eur_mwh": round(all_sorted[int(n*0.10)], 2),
        "p25_eur_mwh": round(all_sorted[int(n*0.25)], 2),
        "p40_eur_mwh": round(all_sorted[int(n*0.40)], 2),
        "p60_eur_mwh": round(all_sorted[int(n*0.60)], 2),
        "p75_eur_mwh": round(all_sorted[int(n*0.75)], 2),
        "p90_eur_mwh": round(all_sorted[int(n*0.90)], 2),
        "min_eur_mwh": round(min(all_sorted), 2),
        "max_eur_mwh": round(max(all_sorted), 2),
        "pct_slots_negative": round(100 * negative_count / n, 2),
        "pct_slots_extreme_ge250": round(100 * extreme_count / n, 2),
        "monthly_avg_eur_mwh": {
            m: round(statistics.mean(v), 2) for m, v in sorted(by_month_avg.items())
        },
    }
    return result


# --- Presnost predpovedi pocasi (pv_estimate_kwh vs skutecna e_day) --------

def analyze_weather_forecast_accuracy():
    """Pro kazdy den, kde mame weather CSV (soucet pv_estimate_kwh) i odhad
    skutecne denni PV energie (integrace ppv1+ppv2 ze stats souboru, viz
    day_pv_energy_wh naplnene v process_record), porovna predpoved vs skutecnost.
    Pozn.: goodwe_stats_* soubory neobsahuji kumulativni citac (e_day), proto
    skutecnou vyrobu odhadujeme numerickou integraci vykonu - u hustych dat
    (vzorek ~kazdou minutu) je to velmi presny odhad."""
    weather_files = sorted(glob.glob(os.path.join(WEATHER_DIR, "20*.csv")))
    day_estimates = {}
    for path in weather_files:
        day = os.path.basename(path)[:10]
        total = 0.0
        found = False
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.strip().split(";")
                    if len(parts) < 5:
                        continue
                    try:
                        total += float(parts[4])
                        found = True
                    except ValueError:
                        continue
        except OSError:
            continue
        if found:
            day_estimates[day] = total

    comparisons = []
    for day in sorted(day_pv_energy_wh.keys()):
        if day not in day_estimates:
            continue
        # vyzadujeme dostatecnou hustotu vzorku (aspon ~1000 = cca 16+ hodin
        # pokryti pri 1 vzorku/min), jinak by integrace byla nespolehliva
        if day_pv_sample_count.get(day, 0) < 1000:
            continue
        estimate = day_estimates[day]
        actual_kwh = day_pv_energy_wh[day] / 1000.0
        comparisons.append({
            "day": day, "estimate_kwh": round(estimate, 2),
            "actual_kwh": round(actual_kwh, 2),
            "n_samples": day_pv_sample_count[day],
            "diff_pct": round(100 * (actual_kwh - estimate) / estimate, 1) if estimate > 0 else None,
        })

    diffs = [c["diff_pct"] for c in comparisons if c["diff_pct"] is not None]
    stats_out = {}
    if diffs:
        diffs_sorted = sorted(diffs)
        stats_out = {
            "mean_diff_pct": round(statistics.mean(diffs), 1),
            "median_diff_pct": round(statistics.median(diffs), 1),
            "stdev_diff_pct": round(statistics.stdev(diffs), 1) if len(diffs) > 1 else None,
            "p10_diff_pct": round(diffs_sorted[int(len(diffs_sorted)*0.10)], 1),
            "p90_diff_pct": round(diffs_sorted[int(len(diffs_sorted)*0.90)], 1),
        }

    return {
        "n_days_compared": len(comparisons),
        "accuracy_stats": stats_out,
        "samples_last_14": comparisons[-14:],
    }



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archives", nargs="*", default=[],
                         help="Nazvy konkretnich archivu k zahrnuti (napr. reports_202501.tgz reports_202507.tgz)")
    parser.add_argument("--all-archives", action="store_true",
                         help="Zpracovat VSECHNY archivy (pomale, cca desitky minut)")
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)

    print("Zpracovavam aktualni (nezabalene) stats soubory...", file=sys.stderr)
    n = 0
    for name, content in iter_current_files():
        process_record(name, content)
        n += 1
        if n % 5000 == 0:
            print(f"  ... {n} souboru", file=sys.stderr)

    archives_to_process = []
    if args.all_archives:
        archives_to_process = sorted(glob.glob(os.path.join(REPORTS_DIR, "reports_*.tgz")))
    elif args.archives:
        archives_to_process = [os.path.join(REPORTS_DIR, a) for a in args.archives]

    for tgz in archives_to_process:
        print(f"Zpracovavam archiv {os.path.basename(tgz)}...", file=sys.stderr)
        cnt = 0
        for name, content in iter_archive_files(tgz):
            process_record(name, content)
            cnt += 1
        print(f"  -> {cnt} zaznamu", file=sys.stderr)

    print("Analyzuji ceny...", file=sys.stderr)
    price_stats = analyze_prices()

    print("Analyzuji presnost predpovedi pocasi...", file=sys.stderr)
    weather_accuracy = analyze_weather_forecast_accuracy()

    summary = {
        "generated_at": datetime.now().isoformat(),
        "records_processed": records_processed,
        "archives_processed": [os.path.basename(a) for a in archives_to_process],
        "night_load_by_month": summarize_night_load(),
        "pv_window_by_month": summarize_pv_window(),
        "price_stats": price_stats,
        "weather_forecast_accuracy": weather_accuracy,
    }

    out_path = os.path.join(RESULTS_DIR, f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"Hotovo. Vysledky (jen agregovane statistiky): {out_path}", file=sys.stderr)
    print(out_path)


if __name__ == "__main__":
    main()

