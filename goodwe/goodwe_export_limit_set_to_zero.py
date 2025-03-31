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
    await inverter.set_grid_export_limit(0)
    print("Grid export set to 0 Watts.")

asyncio.run(set_grid_export_limit_to_zero())

