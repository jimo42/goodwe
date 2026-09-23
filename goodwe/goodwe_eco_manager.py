#!/bin/python3
"""
Univerzální nástroj pro správu ECO mode časovačů (eco_mode_1..4) a operačního
módu střídače GoodWe GW10K-ET.

============================================================================
DŮLEŽITÉ POZNATKY (zjištěno diagnostikou 2026-07-15, viz SERVER_ACCESS.md):
============================================================================
- Každá skupina eco_mode_N se čte/zapisuje jako 12 bytů (formát níže).
- Byte 4 ("on_off": -1=On, 0=Off) je zapisován PŘÍMO uvnitř těchto 12 bytů.
- Setting "eco_mode_N_switch" je READ-ONLY ALIAS na tentýž byte (NE nezávislý
  registr)! Pokus o jeho samostatný zápis (write_setting) po předchozím
  zápisu celé skupiny vedl k poškození dat (power přepsáno na 0, stav "Off"
  přestože switch=1). PROTO: NIKDY nezapisovat eco_mode_N_switch samostatně -
  stačí zakódovat on_off přímo do 12bytového bloku (viz encode_schedule níže).
- set_operation_mode(OperationMode.ECO_CHARGE / ECO_DISCHARGE) v knihovně
  vždy PŘEPÍŠE eco_mode_1 na 24/7 shortcut a vypne switche 2,3,4 - NEPOUŽÍVAT,
  pokud chceme zachovat vlastní rozvrhy ve všech 4 skupinách!
  Pro obecné přepnutí do ECO módu (bez dotčení časovačů) použít
  set_operation_mode(OperationMode.ECO) - ten pouze zapíše work_mode=3.

Byte formát jedné eco_mode_N skupiny (12 bytů, big-endian):
  byte 0    : start_h   (0-23)
  byte 1    : start_m   (0-59)
  byte 2    : end_h     (0-23)
  byte 3    : end_m     (0-59)
  byte 4    : on_off    (-1 = ON, 0 = OFF)                [signed byte]
  byte 5    : day_bits  (bit0=Sun,bit1=Mon,...,bit6=Sat)  [unsigned byte]
  byte 6-7  : power     (int16, -100..100 %; záporné=nabíjení, kladné=vybíjení)
  byte 8-9  : soc       (int16, 0-100 %; cílový SoC pro nabíjení)
  byte 10-11: months    (int16; 0 = všechny měsíce)

============================================================================
POUŽITÍ:
============================================================================
  python3 goodwe_eco_manager.py read
      Vypíše aktuální operační mód a všechny 4 eco_mode skupiny.

  python3 goodwe_eco_manager.py set <N> <start> <end> <days> <power> <soc> <on|off>
      N      = 1..4
      start  = HH:MM (např. 13:00)
      end    = HH:MM
      days   = čárkou oddělený seznam Sun,Mon,Tue,Wed,Thu,Fri,Sat, nebo "All"
      power  = -100..100 (%). Záporné = nabíjení, kladné = vybíjení.
      soc    = 0..100 (%). Cílový SoC (relevantní u nabíjení).
      on|off = zapne/vypne danou skupinu (on_off byte)
      Příklad:
        python3 goodwe_eco_manager.py set 3 13:00 13:05 Sat -20 90 on

  python3 goodwe_eco_manager.py off <N>
      Vypne skupinu N (nastaví on_off=0), ostatní hodnoty (čas/dny/výkon)
      zachová beze změny.

  python3 goodwe_eco_manager.py mode <GENERAL|OFF_GRID|BACKUP|ECO|PEAK_SHAVING|SELF_USE>
      Přepne operační mód střídače. Pro ECO NEDOJDE k žádné změně eco_mode
      skupin (na rozdíl od ECO_CHARGE/ECO_DISCHARGE, které tento nástroj
      záměrně nepodporuje, aby se předešlo nechtěnému přepsání časovačů).

Po každém zápisu se provádí okamžitá verifikace čtením zpět.
"""

import asyncio
import os
import struct
import sys
import configparser

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(script_dir, 'goodwe'))
import goodwe  # noqa: E402

config = configparser.ConfigParser()
config.read(os.path.join(script_dir, '../conf/goodwe.conf'))

DAY_BIT = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
ALL_DAYS = list(DAY_BIT.keys())

MODE_NAMES = {
    "GENERAL": goodwe.OperationMode.GENERAL,
    "OFF_GRID": goodwe.OperationMode.OFF_GRID,
    "BACKUP": goodwe.OperationMode.BACKUP,
    "ECO": goodwe.OperationMode.ECO,
    "PEAK_SHAVING": goodwe.OperationMode.PEAK_SHAVING,
    "SELF_USE": goodwe.OperationMode.SELF_USE,
}


def encode_schedule(start_h, start_m, end_h, end_m, day_names, power_pct, soc_pct, enabled):
    """Sestaví 12 bytů pro jednu eco_mode_N skupinu."""
    day_bits = 0
    for d in day_names:
        day_bits |= (1 << DAY_BIT[d])
    on_off = -1 if enabled else 0
    return struct.pack(">BBBBbBhhh", start_h, start_m, end_h, end_m, on_off,
                        day_bits, power_pct, soc_pct, 0)


def parse_time(text):
    h, m = text.split(":")
    return int(h), int(m)


def parse_days(text):
    if text.strip().lower() == "all":
        return list(ALL_DAYS)
    days = [d.strip() for d in text.split(",") if d.strip()]
    for d in days:
        if d not in DAY_BIT:
            raise ValueError(f"Neznámý den '{d}'. Platné: {', '.join(ALL_DAYS)} nebo 'All'.")
    return days


def decode_days_from_bits(bits):
    """Převede bitovou hodnotu dnů (Schedule.day_bits) na seznam názvů dnů
    použitelný pro encode_schedule()."""
    if bits is None or bits in (-1, 0x7f):
        return list(ALL_DAYS)
    result = []
    for name, bit in DAY_BIT.items():
        if bits & (1 << bit):
            result.append(name)
    return result


async def connect():
    ip_address = config['settings']['ip_address']
    return await goodwe.connect(ip_address)


async def cmd_read():
    inverter = await connect()
    print(f"Inverter model: {inverter.model_name}")
    print(f"Current operation mode: {await inverter.get_operation_mode()!r}")
    for i in range(1, 5):
        eco = await inverter.read_setting(f'eco_mode_{i}')
        print(f"eco_mode_{i}: {eco}")


async def cmd_set(n, start, end, days_text, power, soc, on_off_text):
    n = int(n)
    if n not in (1, 2, 3, 4):
        raise ValueError("N musí být 1, 2, 3 nebo 4.")
    start_h, start_m = parse_time(start)
    end_h, end_m = parse_time(end)
    days = parse_days(days_text)
    power = int(power)
    soc = int(soc)
    if not (-100 <= power <= 100):
        raise ValueError("power musí být v rozsahu -100..100.")
    if not (0 <= soc <= 100):
        raise ValueError("soc musí být v rozsahu 0..100.")
    enabled = on_off_text.strip().lower() in ("on", "1", "true")

    inverter = await connect()
    name = f'eco_mode_{n}'
    print(f"BEFORE {name}: {await inverter.read_setting(name)}")

    raw = encode_schedule(start_h, start_m, end_h, end_m, days, power, soc, enabled)
    print(f"Writing {name}: {raw.hex()}")
    await inverter.write_setting(name, raw)

    print(f"AFTER  {name}: {await inverter.read_setting(name)}")


async def cmd_off(n):
    n = int(n)
    if n not in (1, 2, 3, 4):
        raise ValueError("N musí být 1, 2, 3 nebo 4.")
    inverter = await connect()
    name = f'eco_mode_{n}'
    current = await inverter.read_setting(name)
    print(f"BEFORE {name}: {current}")

    # Zachovat stávající čas/dny/power/soc, jen vypnout on_off bit.
    # POZOR: atribut s bitovou hodnotou dnů se jmenuje "day_bits", NE "days"
    # ("days" je čitelný string, např. "Sat" - nelze ho použít jako bitmasku).
    raw = encode_schedule(current.start_h, current.start_m, current.end_h, current.end_m,
                           decode_days_from_bits(current.day_bits), current.power, current.soc,
                           enabled=False)
    print(f"Writing {name}: {raw.hex()}")
    await inverter.write_setting(name, raw)

    print(f"AFTER  {name}: {await inverter.read_setting(name)}")


async def cmd_mode(mode_name):
    mode_name = mode_name.strip().upper()
    if mode_name not in MODE_NAMES:
        raise ValueError(f"Neznámý mód '{mode_name}'. Platné: {', '.join(MODE_NAMES.keys())}")
    inverter = await connect()
    print(f"BEFORE mode: {await inverter.get_operation_mode()!r}")
    await inverter.set_operation_mode(MODE_NAMES[mode_name])
    print(f"AFTER  mode: {await inverter.get_operation_mode()!r}")


def print_usage():
    print(__doc__)


async def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    action = sys.argv[1]
    args = sys.argv[2:]

    try:
        if action == "read":
            await cmd_read()
        elif action == "set":
            if len(args) != 7:
                raise ValueError("set vyžaduje 7 argumentů: N start end days power soc on|off")
            await cmd_set(*args)
        elif action == "off":
            if len(args) != 1:
                raise ValueError("off vyžaduje 1 argument: N")
            await cmd_off(*args)
        elif action == "mode":
            if len(args) != 1:
                raise ValueError("mode vyžaduje 1 argument: MODE_NAME")
            await cmd_mode(*args)
        else:
            print(f"Neznámá akce '{action}'.\n")
            print_usage()
            sys.exit(1)
    except ValueError as e:
        print(f"CHYBA: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
