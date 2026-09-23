#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
source "$SCRIPT_DIR"/../conf/relay.conf

pole=`curl http://$IP_ADDRESS:8080/98 2>/dev/null`
faze=()
for i in 4 3 2 1
do
	faze[$i]=`[ \`echo $pole | cut -b $i \` -eq 0 ] && echo OFF || echo ON`
	[ $i -eq 4 ] && echo Cerpadlo ${faze[$i]} || echo "Faze $[ 4 - $i ]   ${faze[$i]}"
done

