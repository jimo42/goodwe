#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd $SCRIPT_DIR

PYSCRIPTS_DIR="$SCRIPT_DIR/../goodwe"

#Threshold, pri krerem se nam nevyplati prodavat
THRESHOLD="20"

enable_grid_output(){
	if  `$PYSCRIPTS_DIR/goodwe_export_limit_read2.py | grep 1 >/dev/null` ; then
		$PYSCRIPTS_DIR/goodwe_export_limit_disable.py
	fi
}

disable_grid_output(){
	if  `$PYSCRIPTS_DIR/goodwe_export_limit_read2.py | grep 0 >/dev/null` ; then
		$PYSCRIPTS_DIR/goodwe_export_limit_enable.py
		$PYSCRIPTS_DIR/goodwe_export_limit_set_to_zero.py
	fi
}


DATE_NOW=$(date "+%Y-%m-%d")
HOUR_NOW=$(date +"%k" | tr -d \  )
[[ $HOUR_NOW -eq 0 ]] && HOUR_NOW=24

PRICE_NOW=$(cat "$DATE_NOW".csv | grep ^"$HOUR_NOW"';' | cut -d\; -f2 | tr \, \. )

echo -n "`date` - "

if (( $(echo "$PRICE_NOW < $THRESHOLD" | bc -l) )); then
	echo "Current price $PRICE_NOW is lower than threshold, we should turn off the output to grid."
	disable_grid_output
else
	echo "Current price $PRICE_NOW higher than threshold, we can enable output to grid."
	enable_grid_output
fi


