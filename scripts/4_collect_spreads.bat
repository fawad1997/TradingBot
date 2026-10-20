@echo off
REM Records live bid/ask/spread about once per second for EVERY symbol in config\config.json (broker.symbol_candidates)
REM into PostgreSQL (table ticks). Default 8 hours. Limit symbols: add  --symbols XAUUSDm,XAUUSD247m
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
python -m app.main collect-spreads --minutes 480
pause
