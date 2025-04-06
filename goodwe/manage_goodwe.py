#!/bin/python3

import asyncio
import os
import sys
import configparser

# Workaround for having goodwe libraries in relative path
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(script_dir, 'goodwe'))
import goodwe

# Set path and configuration
config = configparser.ConfigParser()
config.read(os.path.join(script_dir,'../conf/goodwe.conf'))
ip_address = config['settings']['ip_address']

# Main function to connect to inverter
async def connect_inverter():
    return await goodwe.connect(ip_address)

# Function to read inverter data
async def read_inverter_data():
    inverter = await connect_inverter()
    data = await inverter.read_runtime_data()
    for sensor in inverter.sensors():
        if sensor.id_ in data:
            print(f"{sensor.id_}: \t\t {sensor.name} = {data[sensor.id_]} {sensor.unit}")
    await read_export_limit()

# Function for setting energy export limit
async def set_export_limit(value):
    inverter = await connect_inverter()
    await inverter.set_grid_export_limit(value)
    print(f"Setting grid export limit to {value} Watts")
    await read_export_limit()

# Function for turning the export limit ON (no power out)
async def export_limit_enable():
    inverter = await connect_inverter()
    await inverter.write_setting('grid_export', 1)
    print("Setting grid export limit to ON")
    await read_export_limit()

# Function for turning the export limit OFF (power out allowed)
async def export_limit_disable():
    inverter = await connect_inverter()
    await inverter.write_setting('grid_export', 0)
    print("Setting grid export limit to OFF")
    await read_export_limit()

# Read the current state of the export limit attribure (1=limit applied, 0=export allowed) and allowed power
async def read_export_limit():
    inverter = await connect_inverter()
    enabled = await inverter.read_setting('grid_export')
    limit = await inverter.get_grid_export_limit()
    if enabled == 1:
        print(f"Grid export limit is currently ENABLED (limited power out) with limit {limit} Watts")
    else:
        print(f"Grid export limit is currently DISABLED (power export allowed)")

# Read and set inverter modes
async def read_inverter_mode():
    inverter = await connect_inverter()
    current_mode = await inverter.get_operation_mode()
    print(f"Current inverter mode: {current_mode}")

async def set_inverter_mode(mode):
    inverter = await connect_inverter()
    await inverter.set_operation_mode(mode)
    print(f"Setting inverter mode to {mode}")
    await read_inverter_mode()



# Mapping command line arguments to functions
actions = {
    "read_inverter_data": read_inverter_data,
    "export_limit_disable": export_limit_disable,
    "export_limit_enable": export_limit_enable,
    "set_export_limit": set_export_limit,
    "read_export_limit": read_export_limit,
    "read_inverter_mode": read_inverter_mode,
    "set_inverter_mode": set_inverter_mode
}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: manage_goodwe.py ACTION [VALUE]")
        print("Available actions:", ', '.join(actions.keys()))
        sys.exit(1)

    action = sys.argv[1]
    try:
        value = int(sys.argv[2]) if len(sys.argv) > 2 else None
    except ValueError:
        print("Value must be an integer.")
        sys.exit(1)

    if action in actions:
        if action == "set_export_limit" and value is None:
            print("Please provide a value for setting the export limit.")
            sys.exit(1)
        asyncio.run(actions[action]() if value is None else actions[action](value))
    else:
        print("Invalid action. Available actions are:", ', '.join(actions.keys()))

