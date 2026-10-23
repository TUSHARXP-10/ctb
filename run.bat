@echo off
REM Tushar.ai launcher for Windows.  run.bat [doctor|cycle]
cd /d %~dp0
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
if not exist .venv\.deps (
  echo Installing dependencies...
  python -m pip install -q --upgrade pip
  pip install -q -r requirements.txt
  type nul > .venv\.deps
)
if not exist .env (
  copy .env.example .env >nul
  echo Created .env - open it, add your Binance demo API key/secret, then run: run.bat doctor
  exit /b 0
)
python -m tushar %*
