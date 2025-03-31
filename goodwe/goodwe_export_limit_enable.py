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

    # Nastavení hodnoty na 1 (zapnuto)
    await inverter.write_setting('grid_export', 1)
    print("Grid Export Limit byl zapnut.")

asyncio.run(enable_grid_export_limit())

