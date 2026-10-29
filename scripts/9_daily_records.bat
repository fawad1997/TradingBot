@echo off
REM Daily records: yesterday's review (research\reports\daily\<day>.md) and a database backup (backups\).
REM Windows Task Scheduler runs this every morning as "Quantara daily records"; it is also safe to run by hand.
REM Output is appended to logs\daily_records.log.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
echo ==== %date% %time% >> logs\daily_records.log
python tools\daily_review.py >> logs\daily_records.log 2>&1
python tools\backup_db.py >> logs\daily_records.log 2>&1
