#!/bin/bash
LastMonth=`date -d '10 days ago' '+%Y%m'`

cd /home/automatization/goodwe/logs/goodwe-reports
tar -czf reports_"$LastMonth".tgz goodwe_stats_"$LastMonth"*
rm -f goodwe_stats_"$LastMonth"*

