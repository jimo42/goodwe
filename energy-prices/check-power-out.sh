#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd "$SCRIPT_DIR" || exit 1

PYSCRIPTS_DIR="$SCRIPT_DIR/../goodwe"

#Threshold, pri krerem se nam nevyplati prodavat
THRESHOLD="20"

enable_grid_output(){
	if [[ `$PYSCRIPTS_DIR/manage_goodwe.py read_export_limit | grep ENABLED | wc -l` -eq 1 ]] ; then 
		$PYSCRIPTS_DIR/manage_goodwe.py export_limit_disable
	fi
#	if  `$PYSCRIPTS_DIR/goodwe_export_limit_read2.py | grep 1 >/dev/null` ; then
#		$PYSCRIPTS_DIR/goodwe_export_limit_disable.py
#	fi

	# temporary workaroud for water heating when battery is not functional
	# $SCRIPT_DIR/../relatka/vypni_bojler_naplno.sh
}

disable_grid_output(){
	if [[ `$PYSCRIPTS_DIR/manage_goodwe.py read_export_limit | grep DISABLED | wc -l` -eq 1 ]] ; then
		$PYSCRIPTS_DIR/manage_goodwe.py export_limit_enable
		$PYSCRIPTS_DIR/manage_goodwe.py set_export_limit 0
	fi
#	if  `$PYSCRIPTS_DIR/goodwe_export_limit_read2.py | grep 0 >/dev/null` ; then
#		$PYSCRIPTS_DIR/goodwe_export_limit_enable.py
#		$PYSCRIPTS_DIR/goodwe_export_limit_set_to_zero.py
#	fi

	# temporary workaroud for water heating when battery is not functional
	# $SCRIPT_DIR/../relatka/pust_bojler_na_2a3.sh
}


DATE_NOW=$(date "+%Y-%m-%d")
#HOUR_NOW=$(date +"%H:%M")   --- this works fine if we call the script from cron in */15, but doesn't work if called manually not at exact quarter hour, so improving:
MIN_NOW=$(date +"%M")
MIN_QUARTER=$(( 10#$MIN_NOW / 15 * 15 ))
HOUR_NOW=$(date +"%H"):$(printf "%02d" "$MIN_QUARTER")

# if primary file does not exist, use the secondary one
PRICES="$DATE_NOW".csv
if [ ! -f "$PRICES" ] || [ "$(stat -c %s "$PRICES" 2>/dev/null)" -lt 1024 ]; then
	PRICES="$DATE_NOW"_check.csv
	NOTIF=" (secondary prices file used)"
	if [ ! -f "$PRICES" ] || [ "$(stat -c %s "$PRICES" 2>/dev/null)" -lt 1024 ]; then
		echo "Both primary and secondary file with prices do not exist or are corrupted, exiting."
		exit 1
	fi
fi

PRICE_NOW=$(cat "$PRICES" | grep ^"$HOUR_NOW"';' | cut -d\; -f2 | tr \, \. | tr -d '\r')

echo -n "`date` - "

COMPARISON=$(echo "$PRICE_NOW < $THRESHOLD" | bc -l) || {
    echo "Price comparison failed, exiting."
    exit 1
}

if [[ "$COMPARISON" == "1" ]]; then
	echo "Current price $PRICE_NOW is lower than threshold, we should turn off the output to grid""$NOTIF""."
	disable_grid_output
else
	echo "Current price $PRICE_NOW higher than threshold, we can enable output to grid""$NOTIF""."
	enable_grid_output
fi


