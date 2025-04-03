#!/bin/python3

import asyncio
import os
import sys
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(script_dir, 'goodwe'))
import goodwe
import configparser

config = configparser.ConfigParser()
config.read('../conf/goodwe.conf')

async def get_runtime_data():
    ip_address = config['settings']['ip_address']
    inverter = await goodwe.connect(ip_address)
    limit = await inverter.get_grid_export_limit()
    print(f"Current grid export limit: {limit} Watts")

asyncio.run(get_grid_export_limit())

