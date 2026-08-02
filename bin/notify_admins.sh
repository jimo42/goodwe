#!/bin/sh
set -eu

if [ "$#" -ne 0 ]; then
    echo "Použití: text | /home/automatization/goodwe/bin/notify_admins.sh" >&2
    exit 64
fi

exec /usr/bin/python3 \
    /home/automatization/goodwe/bin/whatsapp_spool_client.py \
    notify
