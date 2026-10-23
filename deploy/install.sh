#!/usr/bin/env bash
# Tushar.ai: one-command install on a fresh Ubuntu/Debian server (VPS).
#
#   curl -fsSL https://raw.githubusercontent.com/TUSHARXP-10/ctb/main/deploy/install.sh | bash
#
# It installs Docker, downloads Tushar.ai, asks for your Binance demo keys and your Claude token
# (made on your PC with `run.bat token`), checks that Binance accepts this server's IP address,
# and starts the bot 24/7 with a secure https link for your phone.
set -euo pipefail

REPO="https://github.com/TUSHARXP-10/ctb.git"
DIR="${TUSHAR_DIR:-$HOME/tushar-ai}"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

say() { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }

say "Installing Docker and git (if needed)"
if ! command -v git >/dev/null 2>&1; then $SUDO apt-get update -y && $SUDO apt-get install -y git; fi
if ! command -v docker >/dev/null 2>&1; then curl -fsSL https://get.docker.com | $SUDO sh; fi

say "Downloading Tushar.ai into $DIR"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone "$REPO" "$DIR"; fi
cd "$DIR"

set_env() {  # set_env KEY VALUE  (adds or replaces a line in .env)
  local v
  v=$(printf '%s' "$2" | sed -e 's/[\\|&]/\\&/g')
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$v|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}

if [ ! -f .env ]; then
  cp .env.example .env
  say "Your keys (typed here, saved only in $DIR/.env on this server)"
  read -rp  "Binance DEMO API key: " BK </dev/tty
  read -rsp "Binance DEMO secret key (hidden): " BS </dev/tty; echo
  read -rsp "Claude token from 'run.bat token' on your PC (hidden): " CT </dev/tty; echo
  read -rp  "Telegram bot token (optional, Enter to skip): " TG </dev/tty
  set_env TRADING_MODE demo
  set_env BINANCE_API_KEY "$BK"
  set_env BINANCE_API_SECRET "$BS"
  set_env CLAUDE_CODE_OAUTH_TOKEN "$CT"
  [ -n "$TG" ] && set_env TELEGRAM_BOT_TOKEN "$TG"
  set_env TUSHAR_MODE phone
  chmod 600 .env
fi

mkdir -p data
$SUDO chown -R 1000:1000 data   # the container runs as an unprivileged user (uid 1000)

say "Building (2-4 minutes the first time)"
$SUDO docker compose build

say "Checking Binance, news and Claude from this server"
if ! $SUDO docker compose run --rm tushar python -m tushar doctor; then
  echo
  echo "Something above is marked XX."
  echo " - 'restricted location', 'HTTP 451' or a network error on Binance: Binance blocks this server's IP."
  echo "   Delete this server and try another provider/region, or keep running Tushar.ai on your PC."
  echo " - Claude failed: run 'run.bat token' on your PC again and put the new token in $DIR/.env"
  echo "Fix it, then run this installer again (it keeps your .env)."
  exit 1
fi

say "Starting Tushar.ai (it restarts by itself after crashes and reboots)"
$SUDO docker compose up -d
echo "Waiting for the phone link..."
for i in $(seq 1 30); do
  sleep 3
  if $SUDO docker compose logs tushar 2>/dev/null | grep -q "trycloudflare.com"; then break; fi
done
$SUDO docker compose logs --tail 80 tushar
cat <<MSG

Done. Tushar.ai is trading 24/7 on this server.
 - Phone: scan the QR above or open the https://....trycloudflare.com link, use the access key.
 - The link changes when the bot restarts. See the current one any time with:
     cd $DIR && sudo docker compose logs tushar | grep -A3 "OPEN ON YOUR PHONE"
 - Update later:  cd $DIR && git pull && sudo docker compose up -d --build
 - Stop:          cd $DIR && sudo docker compose down
MSG
