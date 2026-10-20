@echo off
REM Whole system on a SIMULATED market (no MT5, no database needed). Good first test of the dashboard.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
start "" cmd /c "timeout /t 8 >nul & start http://127.0.0.1:8787"
python -m app.main run --simulate
pause
