@echo off
REM ===== Quantara - one-time installation =====
cd /d "%~dp0\.."
echo.
echo  Checking Python...
where py >nul 2>nul && (py -3.12 -V >nul 2>nul && set PY=py -3.12) 
if not defined PY set PY=python
%PY% -c "import sys; assert sys.version_info>=(3,10) and sys.maxsize>2**32, 'Need 64-bit Python 3.10+'; print('Python', sys.version.split()[0], 'OK')"
if errorlevel 1 (
  echo  !! Install 64-bit Python 3.12 from python.org and tick "Add python.exe to PATH". Then run this again.
  pause & exit /b 1
)
if not exist .venv (
  echo  Creating virtual environment .venv ...
  %PY% -m venv .venv || (echo  !! venv failed & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 ( echo  !! Package installation failed - see the messages above. & pause & exit /b 1 )
if not exist .env (
  copy .env.example .env >nul
  echo  Created .env from the template - fill in your MT5 and database details now.
  notepad .env
)
echo.
echo  Installation finished. Next: run 2_check.bat
pause
