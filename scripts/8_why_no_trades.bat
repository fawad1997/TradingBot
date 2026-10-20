@echo off
REM Read-only report: where did signals stop in the last 48 hours (per symbol, per gate)?
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
python -m app.main why-no-trades --hours 48
pause
