#!/usr/bin/env bash
# One-command setup for WhatsApp for Claude. Run it in this folder:
#     bash setup.sh
# You can run it again any time: it keeps your settings, updates what changed and prints
# your two addresses again.
set -e
cd "$(dirname "$0")"

random() { openssl rand -hex "$1" 2>/dev/null || head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
from_telegram() {  # a setting from Telegram for Claude's connector, if it's on this server
  for f in ../telegram-for-claude/telegram-connector/.env /opt/telegram-for-claude/telegram-connector/.env \
           /opt/telegram-connector/.env; do
    if [ -f "$f" ] && grep -q "^$1=." "$f"; then grep "^$1=" "$f" | head -1 | cut -d= -f2-; return; fi
  done
}

echo "== 1/4 Checking Docker =="
if ! command -v docker >/dev/null 2>&1; then
  read -rp "Docker isn't installed. Install it now? (y/N): " ok
  if [ "$ok" = "y" ] || [ "$ok" = "Y" ]; then
    curl -fsSL https://get.docker.com | sh
  else
    echo "Docker is needed. Stopping."; exit 1
  fi
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker is installed but 'docker compose' is missing. Install the Docker Compose plugin."; exit 1
fi
command -v git >/dev/null 2>&1 || apt-get install -y git >/dev/null
echo "Docker is ready."

echo "== 2/4 Settings =="
if [ ! -f .env ]; then
  read -rsp "Paste your Cloudflare tunnel token (it stays hidden), then press Enter: " TOKEN; echo
  if [ -z "$TOKEN" ]; then echo "No token pasted. Run bash setup.sh again."; exit 1; fi
  TZ_DEFAULT="$(from_telegram TIMEZONE)"
  read -rp "Your time zone, e.g. Europe/Istanbul or Asia/Tehran [${TZ_DEFAULT:-UTC}]: " TIMEZONE
  read -rp "The address you gave the tunnel in Cloudflare, e.g. whatsapp.example.com: " DOMAIN
  KEY="$(from_telegram TRANSCRIBE_API_KEY)"
  if [ -n "$KEY" ]; then
    read -rp "Use the same OpenAI key as your Telegram connector for voice messages? (Y/n): " ok
    if [ "$ok" = "n" ] || [ "$ok" = "N" ]; then KEY=""; fi
  fi
  if [ -z "$KEY" ]; then
    read -rsp "OpenAI API key for voice messages (hidden; press Enter to skip): " KEY; echo
  fi
  umask 077
  cat > .env <<EOF
MCP_SECRET=$(random 24)
WHATSAPP_TOKEN=$(random 32)
TUNNEL_TOKEN=$TOKEN
DOMAIN=$DOMAIN
TIMEZONE=${TIMEZONE:-${TZ_DEFAULT:-UTC}}
TRANSCRIBE_API_KEY=$KEY
EOF
  echo "Settings saved (private file .env)."
else
  echo "Settings already exist, keeping them."
fi
if ! grep -q '^STORAGE_LIMIT_MB=' .env; then
  read -rp "Most space the WhatsApp data may use, in MB (oldest data is removed past it) [1024]: " LIMIT
  echo "STORAGE_LIMIT_MB=${LIMIT:-1024}" >> .env
fi
DOMAIN="$(grep '^DOMAIN=' .env | cut -d= -f2)"
MCP_SECRET="$(grep '^MCP_SECRET=' .env | cut -d= -f2)"

echo "== 3/4 Building (the first time takes 5 to 15 minutes) =="
docker compose build

echo "== 4/4 Starting =="
docker compose up -d
docker image prune -f >/dev/null  # old versions of the gateway, after an update
echo
echo "All set! Two addresses. Treat both like passwords."
echo
echo "1) Link your WhatsApp: open this on a computer and scan the QR code with your phone"
echo "   (WhatsApp > Settings > Linked devices > Link a device):"
echo
echo "    https://$DOMAIN/$MCP_SECRET/pair"
echo
echo "2) Your connector address, ONLY for claude.ai > Settings > Connectors > Add custom connector:"
echo
echo "    https://$DOMAIN/$MCP_SECRET/mcp"
