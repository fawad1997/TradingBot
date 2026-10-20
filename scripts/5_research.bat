@echo off
REM Phase 2: download history from MT5 and run the full research package on pullback_v1.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
python -m app.main export-history --start 2020-01-01 || goto :err
python -m app.main import-events config\economic_events.csv
python -m app.main backtest    --strategy pullback_v1 || goto :err
python -m app.main costs       --strategy pullback_v1 || goto :err
python -m app.main exits       --strategy pullback_v1 || goto :err
python -m app.main sessions    --strategy pullback_v1 || goto :err
python -m app.main walkforward --strategy pullback_v1 || goto :err
echo.
echo  Reports are in research\reports  (open the .md files in VS Code or any Markdown viewer)
start "" "%cd%\research\reports"
pause
exit /b 0
:err
echo  !! A research step failed - read the message above.
pause
exit /b 1
