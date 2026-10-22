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
bash scripts/bootstrap_droplet.sh
```

If the repo is already cloned:

```bash
cd /opt/crypto-trading-machine
git pull --ff-only origin main
bash scripts/bootstrap_droplet.sh
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

Before updating a money-handling deployment, disable new entries with
`LIVE_BUYS_ENABLED=false` in `.env`. Leave the independent risk service running
to protect existing positions. Back up the database using SQLite's backup API,
and do not change `LIVE_MODE`, `DRY_RUN`, or `PAPER_TRADING` as part of a code
update.

```bash
systemctl restart crypto-bot
systemctl status crypto-bot --no-pager -l
journalctl -u crypto-bot -f
```

Run `bash scripts/diag_readonly.sh` on the droplet to verify its revision,
health, risk-loop heartbeat, entry halts and recent losses. Do not enable new
live buys solely because tests pass: execution correctness is not evidence of
positive strategy expectancy.

Entry safety controls:

- New orders are sized to the remaining portfolio exposure budget. Pyramid
  additions consume the remaining **total** position, Kelly and stop-risk
  budgets; they do not receive a fresh full-sized allowance or reset existing
  high-water marks and take-profit flags.
- Stop-risk sizing covers the widest configured ATR stop when ATR stops are
  enabled. Gaps, slippage and commissions can still increase actual losses.
- Delayed order-book retries request fresh full strategy ticks, including all
  risk and quality gates, rather than executing a saved signal.
- A portfolio-wide BUY mutex and a final live submission check revalidate
  drawdown, daily loss and position/exposure limits, including correlated
  baskets and the existing stake in a pyramid add. Protective SELLs do not
  acquire the BUY mutex.
- Daily realized loss includes every exit leg in the UTC day for the selected
  mode, without a latest-500-trades cutoff.

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
bash scripts/bootstrap_droplet.sh
```
