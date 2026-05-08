# Deploy to DigitalOcean Droplet

This runbook prepares and runs the bot on Ubuntu.

## 1. Create droplet

- Provider: DigitalOcean
- Region: United States
- Image: Ubuntu 24.04 LTS
- Size: 2 vCPU / 4 GB RAM minimum
- Authentication: SSH key preferred

## 2. Connect

Run from your computer:

```bash
ssh root@YOUR_PUBLIC_IPV4
```

## 3. Bootstrap app

On the droplet:

```bash
git clone https://github.com/digitalinkpact/crypto-trading-machine.git /opt/crypto-trading-machine
cd /opt/crypto-trading-machine
chmod +x scripts/bootstrap_droplet.sh
./scripts/bootstrap_droplet.sh
```

If the repo is already cloned:

```bash
cd /opt/crypto-trading-machine
git pull --ff-only origin main
chmod +x scripts/bootstrap_droplet.sh
./scripts/bootstrap_droplet.sh
```

## 4. Configure environment

Edit environment file and add fresh API keys.

```bash
nano /opt/crypto-trading-machine/.env
```

Keep these values true while testing:

- DRY_RUN=true
- PAPER_TRADING=true

## 5. Restart and verify

```bash
systemctl restart crypto-bot
systemctl status crypto-bot --no-pager -l
journalctl -u crypto-bot -f
```

## 6. Enable app authentication (recommended)

Set both values in .env:

- APP_BASIC_AUTH_USER
- APP_BASIC_AUTH_PASSWORD

Then restart:

```bash
systemctl restart crypto-bot
```

All routes are protected with HTTP Basic auth when both values are set.
Health endpoint remains open at /healthz.

## 7. Security checklist

- Rotate all previously exposed API keys.
- Use exchange keys with no withdrawal permission.
- Keep only required ports open (SSH and app endpoint if needed).
- Keep firewall enabled.
- Run host hardening script once: scripts/harden_server.sh
- Follow docs/SECURITY_HARDENING.md

## 8. Tomorrow quick start

```bash
ssh root@YOUR_PUBLIC_IPV4
cd /opt/crypto-trading-machine
git pull --ff-only origin main
./scripts/bootstrap_droplet.sh
```
