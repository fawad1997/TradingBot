@echo off
REM Starts the trading core + dashboard. Environment (DRY_RUN / SHADOW / DEMO / REAL) comes from config\config.json
REM Stop with Ctrl+C (broker-side stop losses stay in place).
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
start "" cmd /c "timeout /t 12 >nul & start http://127.0.0.1:8787"
python -m app.main run
pause
