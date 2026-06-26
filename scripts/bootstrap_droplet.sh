#!/usr/bin/env bash
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/crypto-trading-machine}
REPO_URL=${REPO_URL:-https://github.com/digitalinkpact/crypto-trading-machine.git}
BRANCH=${BRANCH:-main}
SERVICE_SRC=${SERVICE_SRC:-deploy/systemd/crypto-bot.service}
SERVICE_DST=/etc/systemd/system/crypto-bot.service

if [[ ${EUID} -ne 0 ]]; then
  echo "Please run as root (or with sudo)."
  exit 1
fi

echo "Installing system packages..."
apt update
apt install -y git python3-venv python3-pip

echo "Cloning or updating repository..."
mkdir -p "$(dirname "$APP_DIR")"
if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "Creating virtual environment and installing dependencies..."
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

if [[ ! -f .env ]]; then
  echo "No .env found. Creating .env from .env.example."
  cp .env.example .env
  echo "Edit $APP_DIR/.env with real API keys before enabling live trading."
fi

echo "Installing systemd service..."
cp "$SERVICE_SRC" "$SERVICE_DST"
systemctl daemon-reload
systemctl enable crypto-bot
systemctl restart crypto-bot

echo "Service status:"
systemctl status crypto-bot --no-pager -l || true

echo "Done. Useful commands:"
echo "  journalctl -u crypto-bot -f"
echo "  systemctl restart crypto-bot"
echo "  systemctl status crypto-bot"
