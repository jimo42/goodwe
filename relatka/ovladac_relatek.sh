#!/bin/bash
#https://github.com/nielsonm236/NetMod-ServerApp/wiki

#http://192.168.x.x:8080/XX
#02>2  OFF, 03>2  ON
#16>9  OFF, 17>9  ON
#18>10 OFF, 19>10 ON
#20>11 OFF, 21>11 ON
#60 - get IOcontrol page
#99 - show bit field

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
source "$SCRIPT_DIR/../conf/relay.conf"

LOG="$SCRIPT_DIR/../logs/relatka.log"

if [ "$1" == "s" ]; then $SCRIPT_DIR/status.sh ; echo "`date +%Y-%m-%d_%H:%M` stat: `curl http://$IP_ADDRESS:8080/99 2>/dev/null`" >> "$LOG" ; exit 0; fi

if [ $# -ne 2 ]; then echo "Pozaduji se dva parametry, faze/cerpadlo a ON/OFF"; exit 1; fi
faze=`echo $1 | tr '[:lower:]' '[:upper:]'`
[[ !(( $faze -ge 1 ) && ( $faze -le 3 ) || ( $faze == "C" )) ]] && echo "Faze musi byt 1, 2, 3, nebo C (cerpadlo)" && exit 1
stav=`echo $2 | tr '[:lower:]' '[:upper:]'`
[[ ( "$stav" != "ON" ) && ( "$stav" != "OFF" ) ]] && echo "Druhy parametr musi byt ON nebo OFF" && exit 1


echo "Predchozi stav:"
bash $SCRIPT_DIR/status.sh
LOGPREV=`curl http://$IP_ADDRESS:8080/99 2>/dev/null`

if [ $stav == "ON" ]
then 
	echo -n ON\ 
	[ $faze == "C" ] && port="03" || port=$[ ( $faze + 7 ) * 2 + 1 ]
	echo $faze :: $port
	curl http://$IP_ADDRESS:8080/$port

elif [ $stav == "OFF" ]
	then 
		echo -n OFF\ 
		[ $faze == "C" ] && port="02" || port=$[ ( $faze + 7 ) * 2 ]
	        echo $faze :: $port
		curl http://$IP_ADDRESS:8080/$port
	else 
		echo "K tehle chybe by fakt nemelo dojit"
fi
echo "Novy stav:"
bash $SCRIPT_DIR/status.sh
LOGNEW=`curl http://$IP_ADDRESS:8080/99 2>/dev/null`

echo "`date +%Y-%m-%d_%H:%M` prev: $LOGPREV, cmd: $faze $stav,`[ $stav == "ON" ] && echo " "` new: $LOGNEW" >> "$LOG"

