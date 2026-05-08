# Security Hardening Checklist

Use this checklist before exposing the app on a public server.

## 1. Secrets and keys

- Rotate all previously exposed API keys immediately.
- Use Binance keys with no withdrawal permissions.
- Store secrets only in server-side environment files.
- Lock down env file permissions.

```bash
chown root:root /opt/crypto-trading-machine/.env
chmod 600 /opt/crypto-trading-machine/.env
```

## 2. App protection

The app supports optional HTTP Basic auth for all routes except /healthz.

Set both values in .env:

- APP_BASIC_AUTH_USER
- APP_BASIC_AUTH_PASSWORD

Then restart:

```bash
systemctl restart crypto-bot
```

## 3. Host hardening

Run the hardening script once as root:

```bash
cd /opt/crypto-trading-machine
chmod +x scripts/harden_server.sh
SSH_PORT=22 ALLOW_APP_PORT=8000 ./scripts/harden_server.sh
```

This script:

- Enables UFW with deny-by-default inbound policy
- Enables fail2ban
- Enables unattended security upgrades
- Disables SSH password login and root SSH login

## 4. Non-root app user (recommended)

Create dedicated user and run service under that account.

```bash
adduser --disabled-password --gecos "" cryptobot
chown -R cryptobot:cryptobot /opt/crypto-trading-machine
```

Update service user in deploy/systemd/crypto-bot.service to User=cryptobot, then:

```bash
systemctl daemon-reload
systemctl restart crypto-bot
```

## 5. Access control

- Prefer private access (VPN, IP allowlist, or tunnel).
- If public, place app behind reverse proxy with HTTPS.
- Do not expose admin surface without authentication.

## 6. Monitoring

```bash
systemctl status crypto-bot --no-pager -l
journalctl -u crypto-bot -f
```

Set alerts for service restarts and repeated authentication failures.

## 7. Trading safety

Keep these enabled until fully verified:

- DRY_RUN=true
- PAPER_TRADING=true

Switch to live only after several days of stable paper performance.
