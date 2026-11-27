#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/sales-bot"
REPO_RAW="https://raw.githubusercontent.com/sirvan1133/pasarguard-sales-bot/main"
SERVICE_FILE="/etc/systemd/system/sales-bot.service"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "❌ Please run as root (or with sudo)."
  exit 1
fi

echo "=========================================="
echo "   PasarGuard Telegram Sales Bot Installer"
echo "=========================================="

# Read from /dev/tty so `curl ... | bash` works interactively
read_input() {
  local prompt="$1"
  local var=""
  read -r -p "$prompt" var </dev/tty
  echo "$var"
}

BOT_TOKEN=""
while [[ -z "$BOT_TOKEN" ]]; do
  BOT_TOKEN="$(read_input '🤖 Enter Telegram Bot Token: ')"
done

ADMIN_IDS=""
while [[ -z "$ADMIN_IDS" ]]; do
  ADMIN_IDS="$(read_input '👤 Enter Admin Numeric Telegram ID (comma-separated for multiple): ')"
done

echo ""
echo "📦 Installing dependencies (python3, qrencode, curl)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq python3 qrencode curl

mkdir -p "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR"

echo "⬇️ Downloading bot.py..."
curl -fsSL "$REPO_RAW/bot.py" -o "$INSTALL_DIR/bot.py"
chmod 700 "$INSTALL_DIR/bot.py"
python3 -m py_compile "$INSTALL_DIR/bot.py"

if [[ -f "$INSTALL_DIR/.env" ]]; then
  echo "ℹ️ Existing $INSTALL_DIR/.env found — updating BOT_TOKEN and ADMIN_IDS only..."
  grep -v -E '^(BOT_TOKEN|ADMIN_IDS)=' "$INSTALL_DIR/.env" > "$INSTALL_DIR/.env.tmp" || true
  {
    echo "BOT_TOKEN=$BOT_TOKEN"
    echo "ADMIN_IDS=$ADMIN_IDS"
    cat "$INSTALL_DIR/.env.tmp"
  } > "$INSTALL_DIR/.env"
  rm -f "$INSTALL_DIR/.env.tmp"
else
  cat > "$INSTALL_DIR/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
PANEL_URL=https://your-panel-domain.com:8443
PANEL_USER=sales_bot
PANEL_PASS=CHANGE_ME
GROUP_ID=1
CARD_NUMBER=0000000000000000
CARD_OWNER=Card Owner
USDT_BEP20=0x0000000000000000000000000000000000000000
EOF
fi
chmod 600 "$INSTALL_DIR/.env"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=PasarGuard Telegram Sales Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/bot.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable sales-bot.service
systemctl restart sales-bot.service
sleep 2

echo ""
echo "✅ Installation complete!"
systemctl --no-pager status sales-bot.service | head -n 12
echo ""
echo "📌 Next steps:"
echo "  1. If this is a fresh server, set PANEL_URL, PANEL_USER, PANEL_PASS in $INSTALL_DIR/.env and run: systemctl restart sales-bot.service"
echo "  2. Card number, wallet address, prices, discounts, and trial settings can all be configured inside Telegram via 👑 پنل ادمین."
