#!/bin/bash
set -u

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" >/dev/null 2>&1; pwd )
cd "$SCRIPT_DIR"

EXPECTED_SLOTS=96

log(){
    echo "$*"
}

valid_csv(){
    FILE="$1"
    if [ ! -s "$FILE" ]
    then
        return 1
    fi
    awk -F';' -v expected="$EXPECTED_SLOTS" '
        BEGIN { ok=1 }
        NF != 2 { ok=0 }
        $1 !~ /^[0-2][0-9]:[0-5][0-9]$/ { ok=0 }
        $2 !~ /^-?[0-9]+([,.][0-9]+)?$/ { ok=0 }
        END { if (NR != expected) ok=0; exit(ok ? 0 : 1) }
    ' "$FILE"
}

download_html_snapshot(){
    DESIRED_DATE="$1"
    HTML="$DESIRED_DATE.html"
    URL="https://www.ote-cr.cz/cs/kratkodobe-trhy/elektrina/denni-trh?date=$DESIRED_DATE"
    TMP="$HTML.tmp"
    if curl -fLsS "$URL" -o "$TMP"
    then
        mv "$TMP" "$HTML"
        log "OTE html snapshot downloaded for $DESIRED_DATE."
    else
        rm -f "$TMP"
        log "OTE html snapshot download failed for $DESIRED_DATE."
    fi
}

fetch_ote(){
    DESIRED_DATE="$1"
    CSV="$DESIRED_DATE.csv"
    rm -f "$CSV.tmp"
    if ./getPricesFromOTE.py "$DESIRED_DATE"
    then
        if valid_csv "$CSV"
        then
            log "OTE csv valid for $DESIRED_DATE."
            return 0
        fi
        log "OTE csv invalid for $DESIRED_DATE."
        rm -f "$CSV"
    else
        log "OTE fetch failed for $DESIRED_DATE."
    fi
    return 1
}

fetch_entsoe(){
    DESIRED_DATE="$1"
    CHECK="$DESIRED_DATE"_check.csv
    rm -f "$CHECK.tmp"
    if ./getPricesFromENTSOE.py "$DESIRED_DATE"
    then
        if valid_csv "$CHECK"
        then
            log "ENTSO-E csv valid for $DESIRED_DATE."
            return 0
        fi
        log "ENTSO-E csv invalid for $DESIRED_DATE."
        rm -f "$CHECK"
    else
        log "ENTSO-E fetch failed for $DESIRED_DATE."
    fi
    return 1
}

get_file(){
    DESIRED_DATE="$1"
    CSV="$DESIRED_DATE.csv"
    CHECK="$DESIRED_DATE"_check.csv

    log "Processing $DESIRED_DATE."
    download_html_snapshot "$DESIRED_DATE"

    fetch_ote "$DESIRED_DATE"
    OTE_RC=$?

    if valid_csv "$CHECK"
    then
        log "Existing ENTSO-E check csv is valid for $DESIRED_DATE."
        ENTSOE_RC=0
    else
        rm -f "$CHECK"
        fetch_entsoe "$DESIRED_DATE"
        ENTSOE_RC=$?
    fi

    if valid_csv "$CSV"
    then
        if valid_csv "$CHECK"
        then
            if diff -q "$CSV" "$CHECK" >/dev/null 2>&1
            then
                log "OTE and ENTSO-E CSVs match for $DESIRED_DATE; removing redundant check file."
                rm -f "$CHECK"
            else
                log "WARNING: OTE and ENTSO-E CSVs differ for $DESIRED_DATE; keeping both files."
            fi
        fi
        return 0
    fi

    if valid_csv "$CHECK"
    then
        log "Primary OTE CSV missing, but ENTSO-E fallback is valid for $DESIRED_DATE."
        return 0
    fi

    log "ERROR: no valid price CSV from either source for $DESIRED_DATE."
    return 1
}

log "$(date)"

TODAY=$(date "+%Y-%m-%d" --date "today")
if valid_csv "$TODAY.csv"
then
    log "Today's primary file exists and is valid."
else
    log "Today's primary file missing or invalid."
    get_file "$TODAY"
fi

TOMORROW=$(date "+%Y-%m-%d" --date "tomorrow")
if valid_csv "$TOMORROW.csv"
then
    log "Tomorrow's primary file exists and is valid."
else
    log "Tomorrow's primary file missing or invalid."
    if [ "$(date +"%H")" -ge 14 ]
    then
        get_file "$TOMORROW"
    else
        log "Tomorrow prices are not requested before 14:00 local time."
    fi
fi
