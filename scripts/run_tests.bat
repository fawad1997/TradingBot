@echo off
REM Unit + chaos + integration tests. Add TEST_DATABASE_URL (a THROW-AWAY database) to also test PostgreSQL.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
python -m unittest discover -s tests -t . -v
pause
