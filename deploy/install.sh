#!/usr/bin/env bash
# Tushar.ai: one-command install on a fresh Ubuntu/Debian server (VPS). Works from a phone:
# open your server's browser console and paste
#
#   curl -fsSL https://raw.githubusercontent.com/TUSHARXP-10/ctb/main/deploy/install.sh | bash
#
# It installs Docker, downloads Tushar.ai, asks for your Binance demo keys, logs in to Claude
# (you approve on your phone), checks that Binance accepts this server's IP address, and starts
# the bot 24/7 with a secure https link for your phone. Run it again any time to update.
set -euo pipefail

REPO="https://github.com/TUSHARXP-10/ctb.git"
DIR="${TUSHAR_DIR:-$HOME/tushar-ai}"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

say() { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }
ask() { local v; read -r -p "$1" v </dev/tty; printf '%s' "$v" | tr -d '[:space:]'; }

say "Installing Docker and git (if needed)"
if ! command -v git >/dev/null 2>&1; then $SUDO apt-get update -y && $SUDO apt-get install -y git; fi
if ! command -v docker >/dev/null 2>&1; then curl -fsSL https://get.docker.com | $SUDO sh; fi

# Small servers: add swap so the build and Claude never run out of memory.
if [ "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)" -lt 3500 ] && [ "$(awk '/SwapTotal/ {print $2}' /proc/meminfo)" -eq 0 ]; then
  say "Adding 2 GB of swap"
  $SUDO fallocate -l 2G /swapfile 2>/dev/null || $SUDO dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  $SUDO chmod 600 /swapfile && $SUDO mkswap /swapfile >/dev/null && $SUDO swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
fi

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
  say "Binance Demo keys (demo.binance.com > API Management > Create API, tick Futures)"
  echo "Paste them here. They are saved only in $DIR/.env on this server."
  BK=$(ask "Binance DEMO API key: ")
  BS=$(ask "Binance DEMO secret key: ")
  TG=$(ask "Telegram bot token (optional, press Enter to skip): ")
  set_env TRADING_MODE demo
  set_env BINANCE_API_KEY "$BK"
  set_env BINANCE_API_SECRET "$BS"
  [ -n "$TG" ] && set_env TELEGRAM_BOT_TOKEN "$TG"
  set_env TUSHAR_MODE phone
  chmod 600 .env
fi

mkdir -p data
$SUDO chown -R 1000:1000 data   # the container runs as an unprivileged user (uid 1000)

say "Building (3-6 minutes the first time)"
$SUDO docker compose build

if ! grep -q '^CLAUDE_CODE_OAUTH_TOKEN=sk-' .env && [ ! -s data/claude_token.txt ]; then
  say "Claude login (uses your Pro/Max subscription, no API key)"
  echo "A link appears below. Open it on your phone, approve with your Claude account,"
  echo "copy the code it shows, come back here and paste it, then press Enter."
  $SUDO docker compose run --rm -it tushar python -m tushar token --save </dev/tty
fi

say "Checking Binance, news and Claude from this server"
if ! $SUDO docker compose run --rm tushar python -m tushar doctor; then
  echo
  echo "Something above is marked XX:"
  echo " - 'restricted location', 'HTTP 451' or a network error on Binance: Binance blocks this server's IP."
  echo "   Destroy this server and create one with another provider or region."
  echo " - Invalid API-key: check the keys in $DIR/.env (nano $DIR/.env), keys must be from demo.binance.com."
  echo " - Claude failed: run   cd $DIR && sudo docker compose run --rm -it tushar python -m tushar token --save"
  echo "Then run this installer again (it keeps your settings)."
  exit 1
fi

say "Starting Tushar.ai (restarts by itself after crashes and reboots)"
$SUDO docker compose up -d
echo "Waiting for the phone link..."
for _ in $(seq 1 40); do
  sleep 3
  if $SUDO docker compose logs tushar 2>/dev/null | grep -q "trycloudflare.com"; then break; fi
done
LINK=$($SUDO docker compose logs tushar 2>/dev/null | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | grep -v '^https://api\.' | tail -1 || true)
KEY=$(cat data/access_key.txt 2>/dev/null || true)
cat <<MSG

==================================================================
 Tushar.ai is trading 24/7 on this server.

 Open on your phone:  ${LINK:-(not ready yet: see the logs command below)}
 Access key:          ${KEY:-see data/access_key.txt}
==================================================================

 Show the link again:  cd $DIR && sudo docker compose logs tushar | grep -A4 "OPEN ON YOUR PHONE"
 Live logs:            cd $DIR && sudo docker compose logs -f --tail 50 tushar
 Update:               run this installer again
 Stop:                 cd $DIR && sudo docker compose down
 The link changes when the bot restarts (use the "Show the link" command).
MSG
