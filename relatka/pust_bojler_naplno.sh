#!/bin/bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

"$SCRIPT_DIR"/ovladac_relatek.sh 1 on
"$SCRIPT_DIR"/ovladac_relatek.sh 2 on
"$SCRIPT_DIR"/ovladac_relatek.sh 3 on

sleep 120m
"$SCRIPT_DIR"/ovladac_relatek.sh c on
sleep 5m
"$SCRIPT_DIR"/ovladac_relatek.sh c on

