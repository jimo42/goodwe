#!/bin/python3
"""Diagnostic read-only script to inspect eco_mode_1..4 groups and their switches.
Does NOT write anything - pure read for exploration purposes."""

import asyncio
import os
import sys
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(script_dir, 'goodwe'))
import goodwe
import configparser

config = configparser.ConfigParser()
config.read(os.path.join(script_dir, '../conf/goodwe.conf'))


async def main():
    ip_address = config['settings']['ip_address']
    inverter = await goodwe.connect(ip_address)

    print(f"Inverter model: {inverter.model_name}")
    mode = await inverter.get_operation_mode()
    print(f"Current operation mode: {mode!r}")

    for i in range(1, 5):
        eco = await inverter.read_setting(f'eco_mode_{i}')
        switch = await inverter.read_setting(f'eco_mode_{i}_switch')
        print(f"eco_mode_{i}: {eco}  (raw switch={switch})")

asyncio.run(main())
