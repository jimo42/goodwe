#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

"$SCRIPT_DIR"/ovladac_relatek.sh 1 off
"$SCRIPT_DIR"/ovladac_relatek.sh 2 off
"$SCRIPT_DIR"/ovladac_relatek.sh 3 off
"$SCRIPT_DIR"/ovladac_relatek.sh c off

