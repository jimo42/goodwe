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
    runtime_data = await inverter.read_runtime_data()

    for sensor in inverter.sensors():
        if sensor.id_ in runtime_data:
            print(f"{sensor.id_}: \t\t {sensor.name} = {runtime_data[sensor.id_]} {sensor.unit}")


asyncio.run(get_runtime_data())

