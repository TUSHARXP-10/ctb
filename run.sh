#!/usr/bin/env bash
# Tushar.ai launcher for macOS / Linux.
#   ./run.sh           start the bot + dashboard
#   ./run.sh doctor    check Binance, news and your Claude login
#   ./run.sh cycle     run one Claude cycle in the terminal
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  $PY -m venv .venv
fi
. .venv/bin/activate
if [ ! -f .venv/.deps ] || [ requirements.txt -nt .venv/.deps ]; then
  echo "Installing dependencies..."
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
  touch .venv/.deps
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Created .env - open it, add your Binance demo API key/secret, then run ./run.sh doctor"
  exit 0
fi
exec python -m tushar "$@"
