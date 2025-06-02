#!/usr/bin/env python3
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import sys

# read token from a file
# to get a token, register (for free) on the web https://transparency.entsoe.eu and then transparency@entsoe.eu
TOKEN_FILE = "/home/jimo/rizeni-elektrarny/conf/entsoe-token"
try:
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        TOKEN = f.read().strip()
except Exception as e:
    print(f"Error while reading the token from file {TOKEN_FILE}: {e}")
    sys.exit(1)

# Constants
EIC_CZ = "10YCZ-CEPS-----N"

# Get argument data
if len(sys.argv) != 2:
    print("Použití: script.py YYYY-MM-DD")
    sys.exit(1)

try:
    local_date = datetime.strptime(sys.argv[1], "%Y-%m-%d")
except ValueError:
    print("Wrong date, use format YYYY-MM-DD")
    sys.exit(1)

# Transform to UTC (request needs to be done in UTC)
start_utc = (local_date - timedelta(hours=2)).strftime("%Y%m%d%H%M")
end_utc = (local_date + timedelta(days=1) - timedelta(hours=2)).strftime("%Y%m%d%H%M")

# --- ENTSO-E API request ---
url = "https://web-api.tp.entsoe.eu/api"
params = {
    "securityToken": TOKEN,
    "documentType": "A44",
    "in_Domain": EIC_CZ,
    "out_Domain": EIC_CZ,
    "periodStart": start_utc,
    "periodEnd": end_utc
}

try:
    response = requests.get(url, params=params)
    response.raise_for_status()
except Exception as e:
    print(f"Error while querying ENTSO-E API: {e}")
    sys.exit(1)

# Parsing XML reply
try:
    root = ET.fromstring(response.content)
except ET.ParseError as e:
    print(f"Error parsing XML: {e}")
    sys.exit(1)

# Get the prices from the XML
prices = {}
for point in root.findall(".//{*}Point"):
    position_el = point.find("{*}position")
    price_el = point.find("{*}price.amount")
    if position_el is not None and price_el is not None:
        position = int(position_el.text)
        price_float = float(price_el.text.replace(",", "."))
        price_str = f"{price_float:.2f}".replace(".", ",")
        prices[position] = price_str

# Export to CSV
out_file = f"{local_date.date()}_check.csv"
with open(out_file, "w", encoding="utf-8") as f:
    for hour in range(1, 25):
        value = prices.get(hour, "")
        f.write(f"{hour};{value}\n")

print(f"Done: {out_file}")

