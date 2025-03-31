#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd $SCRIPT_DIR

./ovladac_relatek.sh 1 on
./ovladac_relatek.sh 2 on
./ovladac_relatek.sh 3 on
sleep 2h
./ovladac_relatek.sh c on
sleep "$[ $1 - 2 ]"h

./ovladac_relatek.sh 1 off
./ovladac_relatek.sh 2 off
./ovladac_relatek.sh 3 off
./ovladac_relatek.sh c off

