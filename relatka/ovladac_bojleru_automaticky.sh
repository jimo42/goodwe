#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
source "$SCRIPT_DIR"/../conf/relay.conf

# Constants - hysteresis thresholds
UPPER_THRESHOLD_1=76
UPPER_THRESHOLD_2=86
UPPER_THRESHOLD_3=96
HYSTERESIS=3


# Lower limits calculation
LOWER_THRESHOLD_1=$[ $UPPER_THRESHOLD_1 - $HYSTERESIS ]
LOWER_THRESHOLD_2=$[ $UPPER_THRESHOLD_2 - $HYSTERESIS ]
LOWER_THRESHOLD_3=$[ $UPPER_THRESHOLD_3 - $HYSTERESIS ]


# First we read the current battery status and relays status (how many phases are currently on for the heater)
BATTERY_CHARGE_STATE=`$SCRIPT_DIR/../goodwe/read_goodwe.py | grep ^battery_soc: | cut -d\= -f2 | cut -d\  -f2`
RELAY_STATE=`curl http://$IP_ADDRESS:8080/99 2>/dev/null`
RUNNING_PHASES=`echo $RELAY_STATE | cut -b1-3 | grep -o 1 | wc -l`

# And now comes the logic... we're going up and down respecting the hysteresis, see thresholds above

ACTION="Default"
if [[ $BATTERY_CHARGE_STATE -ge $UPPER_THRESHOLD_3 ]] || ([[ $BATTERY_CHARGE_STATE -ge $LOWER_THRESHOLD_3 ]] && [[ $RUNNING_PHASES -eq 3 ]]); then
    if [[ $RUNNING_PHASES -ne 3 ]]; then
	[[ `echo $RELAY_STATE | cut -b3` -eq 0 ]] && $SCRIPT_DIR/ovladac_relatek.sh 1 on
	[[ `echo $RELAY_STATE | cut -b2` -eq 0 ]] && $SCRIPT_DIR/ovladac_relatek.sh 2 on
	[[ `echo $RELAY_STATE | cut -b1` -eq 0 ]] && $SCRIPT_DIR/ovladac_relatek.sh 3 on
	ACTION="Battery: $BATTERY_CHARGE_STATE %, heater runs on 3 phases, previously on $RUNNING_PHASES"
    fi
elif [[ $BATTERY_CHARGE_STATE -ge $UPPER_THRESHOLD_2 ]] && [[ $BATTERY_CHARGE_STATE -lt $LOWER_THRESHOLD_3 ]] || ([[ $BATTERY_CHARGE_STATE -ge $LOWER_THRESHOLD_2 ]] && [[ $BATTERY_CHARGE_STATE -lt $UPPER_THRESHOLD_3 ]] && [[ $RUNNING_PHASES -eq 2 ]]); then
    if [[ $RUNNING_PHASES -ne 2 ]]; then
	[[ `echo $RELAY_STATE | cut -b3` -eq 1 ]] && $SCRIPT_DIR/ovladac_relatek.sh 1 off
	[[ `echo $RELAY_STATE | cut -b2` -eq 0 ]] && $SCRIPT_DIR/ovladac_relatek.sh 2 on
	[[ `echo $RELAY_STATE | cut -b1` -eq 0 ]] && $SCRIPT_DIR/ovladac_relatek.sh 3 on
	ACTION="Battery: $BATTERY_CHARGE_STATE %, heater runs on 2 phases, previously on $RUNNING_PHASES"
    fi
elif [[ $BATTERY_CHARGE_STATE -ge $UPPER_THRESHOLD_1 ]] && [[ $BATTERY_CHARGE_STATE -lt $LOWER_THRESHOLD_2 ]] || ([[ $BATTERY_CHARGE_STATE -ge $LOWER_THRESHOLD_1 ]] && [[ $BATTERY_CHARGE_STATE -lt $UPPER_THRESHOLD_2 ]] && [[ $RUNNING_PHASES -eq 1 ]]); then
    if [[ $RUNNING_PHASES -ne 1 ]]; then
	[[ `echo $RELAY_STATE | cut -b3` -eq 1 ]] && $SCRIPT_DIR/ovladac_relatek.sh 1 off
	[[ `echo $RELAY_STATE | cut -b2` -eq 1 ]] && $SCRIPT_DIR/ovladac_relatek.sh 2 off
	[[ `echo $RELAY_STATE | cut -b1` -eq 0 ]] && $SCRIPT_DIR/ovladac_relatek.sh 3 on
	ACTION="Battery: $BATTERY_CHARGE_STATE %, heater runs on 1 phase, previously on $RUNNING_PHASES"
    fi
elif [[ $BATTERY_CHARGE_STATE -lt $LOWER_THRESHOLD_1 ]]; then
    if [[ $RUNNING_PHASES -ne 0 ]]; then
	[[ `echo $RELAY_STATE | cut -b3` -eq 1 ]] && $SCRIPT_DIR/ovladac_relatek.sh 1 off
	[[ `echo $RELAY_STATE | cut -b2` -eq 1 ]] && $SCRIPT_DIR/ovladac_relatek.sh 2 off
	[[ `echo $RELAY_STATE | cut -b1` -eq 1 ]] && $SCRIPT_DIR/ovladac_relatek.sh 3 off
	ACTION="Battery: $BATTERY_CHARGE_STATE %, heater is now turned off, previously running on $RUNNING_PHASES phases"
    fi
fi

# If there was a change, we log it
[[ "$ACTION" != "Default" ]] && echo "`date +%Y-%m-%d_%H:%M` $ACTION" >> $SCRIPT_DIR/../logs/relay.log

