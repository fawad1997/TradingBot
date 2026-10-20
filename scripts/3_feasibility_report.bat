@echo off
REM Phase 0: can 100 USD trade / test XAUUSD on YOUR account? (report also saved in research\reports)
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
python -m app.main feasibility --equity 100
python -m app.main feasibility
pause
