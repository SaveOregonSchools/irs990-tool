@echo off
echo This may take a few minutes, please wait . . . 
cd /d C:\projects\irs990-tool

if not exist exports mkdir exports

sqlite3 -header -csv db\irs990.db ".read sql\remaining_ai_queue_by_amount_bucket.sql" > exports\remaining_ai_queue_by_amount_bucket.csv

echo Done.
echo Wrote exports\remaining_ai_queue_by_amount_bucket.csv
pause