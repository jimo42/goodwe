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
	if [ $? -eq 0 ] ; then echo -n " File downloaded." ; else echo -n " Error while downloading the file."; fi
#	DATE_READ=`cat $HTML | grep serverDataUrl | grep 'kratkodobe-trhy/elektrina/denni-trh/' | cut -d\= -f3 | cut -d\" -f1`
	DATE_READ=`cat $HTML | grep serverDataUrl | grep 'kratkodobe-trhy/elektrina/denni-trh/' | cut -d\= -f3 | cut -d\" -f1 | cut -d\& -f1`
	if [ "$DESIRED_DATE" != "$DATE_READ" ] ; then echo -n " Dates don't match."; exit 1; fi
	cat "$HTML" | pup 'table.report_table tbody tr json{}' | jq -r '.[] | [.children[0].text, .children[1].text] | join(";")' | grep ^[0-9]  \
	| while read L; do echo `echo $L | cut -d\- -f1`\;`echo $L | cut -d\; -f2`; done     > $CSV
	if [ ! -s "$DATE"".csv" ] ; then echo -n " CSV has zero size, will be re-attempted during next execution."; fi

# check from second source
	./getPricesFromENTSOE.py "$DESIRED_DATE"
	diff $CSV "$DESIRED_DATE"_check.csv
	if [ $? -ne 0 ] ; then echo -n " CSVs from two sources differ!"; else echo " CSVs from both sources are the same, deleting the check one."; rm "$DESIRED_DATE"_check.csv; fi
}

DATE=`date "+%Y-%m-%d" --date "today"`
if [ -f "$DATE"".csv" ]; then
    if [ ! -s "$DATE"".csv" ] ; then 
	echo -n "Today's file exist, but has zero size. Will try to download it again."
	get_file "$DATE"
    else
	echo -n "Today's file exist."
    fi
else
    echo -n "Today's file does not exist."
    get_file "$DATE"
fi

DATE=`date "+%Y-%m-%d" --date "tomorrow"`
if [ -f "$DATE"".csv" ]; then
    echo " Tomorrow's file exist."
else
    echo " Tomorrow's file does not exist."
    if [ $(date +"%H") -ge 14 ] ; then get_file "$DATE" ; fi
fi

