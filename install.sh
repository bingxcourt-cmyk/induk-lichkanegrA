#!/usr/bin/env bash
# Установка на Ubuntu/Debian: bash install.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
python3 -m venv venv
venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env

cat > /etc/systemd/system/signalbot.service <<UNIT
[Unit]
Description=Telegram signal copier
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python -m signalbot.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
echo
echo "Готово. Дальше:"
echo "  1) nano $DIR/.env              — заполните API_ID, API_HASH, BOT_TOKEN"
echo "  2) cd $DIR && venv/bin/python -m signalbot.login   — вход в аккаунт-читатель"
echo "  3) systemctl enable --now signalbot"
echo "  4) в боте: /start → выберите каналы «откуда» и «куда»"
