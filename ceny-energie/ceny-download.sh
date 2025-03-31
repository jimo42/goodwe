#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd "$SCRIPT_DIR"

echo -n "`date` "

get_file(){
	DESIRED_DATE="$1"
	HTML="$DESIRED_DATE.html"
	CSV="$DESIRED_DATE.csv"
	URL="https://www.ote-cr.cz/cs/kratkodobe-trhy/elektrina/denni-trh?date=$DESIRED_DATE"
	curl -s "$URL" > "$HTML"
	if [ $? -eq 0 ] ; then echo -n " Soubor stažen." ; else echo -n " Chyba při stažení souboru."; fi
	DATE_READ=`cat $HTML | grep serverDataUrl | grep 'kratkodobe-trhy/elektrina/denni-trh/' | cut -d\= -f3 | cut -d\" -f1`
	if [ "$DESIRED_DATE" != "$DATE_READ" ] ; then echo -n " Data nesouhlasí."; exit 1; fi
	cat "$HTML" | pup 'table.report_table tbody tr json{}' | jq -r '.[] | [.children[0].text, .children[1].text] | join(";")' | grep ^[0-9] > $CSV
	if [ ! -s "$DATE"".csv" ] ; then echo -n " CSV má nulovou velikost, další pokus při příštím běhu."; fi
}

DATE=`date "+%Y-%m-%d" --date "today"`
if [ -f "$DATE"".csv" ]; then
    if [ ! -s "$DATE"".csv" ] ; then 
	echo -n "Soubor dneška existuje, ale má nulovou velikost, zkusíme stáhnout znovu."
	get_file "$DATE"
    else
	echo -n "Soubor dneška existuje."
    fi
else
    echo -n "Soubor dneška neexistuje."
    get_file "$DATE"
fi

DATE=`date "+%Y-%m-%d" --date "tomorrow"`
if [ -f "$DATE"".csv" ]; then
    echo -n " Soubor zítřka existuje."
else
    echo -n " Soubor zítřka neexistuje."
    if [ $(date +"%H") -ge 14 ] ; then get_file "$DATE" ; fi
fi

echo

