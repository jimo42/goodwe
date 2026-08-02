#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "Použití: text | /home/automatization/goodwe/bin/reply_whatsapp.sh REQUEST_JSON" >&2
    exit 64
fi

exec /usr/bin/python3 \
    /home/automatization/goodwe/bin/whatsapp_spool_client.py \
    reply "$1"
