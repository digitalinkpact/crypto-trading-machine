#!/usr/bin/env bash
# One-shot deploy: pull latest code, install deps, configure live mode, start service.
# Usage (from the DigitalOcean console or any root shell on the droplet):
#   curl -sL https://raw.githubusercontent.com/digitalinkpact/crypto-trading-machine/main/scripts/deploy-now.sh | bash
set -euo pipefail

APP_DIR=/opt/crypto-trading-machine
REPO_URL=https://github.com/digitalinkpact/crypto-trading-machine.git
BRANCH=main
SERVICE_SRC=$APP_DIR/deploy/systemd/crypto-bot.service
SERVICE_DST=/etc/systemd/system/crypto-bot.service
AGENT_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA5mb9ywXyeOTnCx2edn9XEsiURKxgBLgpvmN0x0XAWd copilot-deploy"

echo "=== [1/7] Authorise deploy key ==="
mkdir -p ~/.ssh && chmod 700 ~/.ssh
grep -qxF "$AGENT_PUBKEY" ~/.ssh/authorized_keys 2>/dev/null \
  || echo "$AGENT_PUBKEY" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

echo "=== [2/7] Install system packages ==="
apt-get update -qq
apt-get install -y -qq git python3-venv python3-pip

echo "=== [3/7] Clone or pull repository ==="
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR"
  git fetch origin "$BRANCH"
  git reset --hard "origin/$BRANCH"
else
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi
cd "$APP_DIR"
echo "  -> $(git log -1 --oneline)"

echo "=== [4/7] Install Python dependencies ==="
python3 -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate
pip install -q -U pip
pip install -q -r requirements.txt

echo "=== [5/7] Configure .env for LIVE mode ==="
[ -f .env ] || cp .env.example .env

# Ensure live-mode flags are set
_setenv() { grep -q "^$1=" .env && sed -i "s|^$1=.*|$1=$2|" .env || echo "$1=$2" >> .env; }
_setenv LIVE_MODE   true
_setenv DRY_RUN     false
_setenv PAPER_TRADING false

echo "  -> $(grep -E '^(LIVE_MODE|DRY_RUN|PAPER_TRADING)' .env)"

echo "=== [6/7] Install and (re)start systemd service ==="
cp "$SERVICE_SRC" "$SERVICE_DST"
systemctl daemon-reload
systemctl enable crypto-bot
systemctl restart crypto-bot

echo "=== [7/7] Status ==="
sleep 2
systemctl status crypto-bot --no-pager -l || true
echo ""
echo ">>> DEPLOY COMPLETE — live on $(hostname) $(date) <<<"
echo ">>> Tail logs: journalctl -u crypto-bot -f"
