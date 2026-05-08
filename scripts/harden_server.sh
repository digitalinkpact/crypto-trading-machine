#!/usr/bin/env bash
set -euo pipefail

# Basic Ubuntu server hardening for a single-app host.
# Run as root after first SSH login.

if [[ ${EUID} -ne 0 ]]; then
  echo "Please run as root (or with sudo)."
  exit 1
fi

SSH_PORT=${SSH_PORT:-22}
ALLOW_APP_PORT=${ALLOW_APP_PORT:-8000}

echo "Installing hardening packages..."
apt update
DEBIAN_FRONTEND=noninteractive apt install -y \
  ufw fail2ban unattended-upgrades apt-listchanges

echo "Configuring unattended security upgrades..."
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'CFG'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
APT::Periodic::Unattended-Upgrade "1";
CFG

# SSH daemon hardening: no password auth, no root login.
if [[ -f /etc/ssh/sshd_config ]]; then
  cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%Y%m%d%H%M%S)
  sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
  sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
  sed -i 's/^#\?ChallengeResponseAuthentication .*/ChallengeResponseAuthentication no/' /etc/ssh/sshd_config
  if ! grep -q '^PubkeyAuthentication' /etc/ssh/sshd_config; then
    echo 'PubkeyAuthentication yes' >> /etc/ssh/sshd_config
  else
    sed -i 's/^#\?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
  fi
fi

# Restrictive firewall policy.
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp"
ufw allow "${ALLOW_APP_PORT}/tcp"
ufw --force enable

# Fail2ban default jail.
systemctl enable fail2ban
systemctl restart fail2ban

systemctl restart ssh || systemctl restart sshd

echo

echo "Hardening complete."
echo "UFW status:"
ufw status verbose || true
echo
echo "Fail2ban status:"
fail2ban-client status || true
echo
echo "IMPORTANT: Ensure your deployment uses a non-root user and SSH keys."
