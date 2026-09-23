#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
[ "`$SCRIPT_DIR/status.sh  | grep Cerpadlo | cut -d\  -f2`" == "ON" ] && echo "Cerpadlo zrovna bezi, nedelame nic" && exit 1
re='^[0-9]+$'
[[ !( $1 =~ $re ) ]] && echo "Ocekava se 1 parametr, pocet minut behu cerpadla" && exit 1
#[[ ( "a"$1 == "a" ) || ( 0$1 -le 0 ) ]] && echo "Ocekava se 1 parametr, pocet minut behu cerpadla" && exit 1
echo "Zadan cas behu spusteni cerpadla $1 minut."
echo "Poustim cerpado, `date`"
$SCRIPT_DIR/ovladac_relatek.sh C ON
sleep "$1"m
echo "Vypinam cerpado, `date`"
$SCRIPT_DIR/ovladac_relatek.sh C OFF


