#!/bin/python3

import asyncio
import sys
sys.path.append('goodwe')
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

