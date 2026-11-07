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

```bash
systemctl restart crypto-bot
systemctl status crypto-bot --no-pager -l
journalctl -u crypto-bot -f
```

## 6. Enable app authentication (required)

Set `SESSION_SECRET` (and `BASE_URL`, `FORCE_HTTPS` behind a proxy) in .env, then
restart `crypto-bot`. All routes except `/healthz` and `/health` require a login
session. See docs/SECURITY_HARDENING.md.

## 7. Security checklist

- Rotate all previously exposed API keys.
- Use exchange keys with no withdrawal permission.
- Keep only required ports open (SSH and app endpoint if needed).
- Keep firewall enabled.
- Run host hardening script once: scripts/harden_server.sh
- Follow docs/SECURITY_HARDENING.md

## 8. Update an existing droplet

```bash
ssh root@YOUR_PUBLIC_IPV4
cd /opt/crypto-trading-machine
git pull --ff-only origin main
bash scripts/bootstrap_droplet.sh        # reinstalls deps + unit, restarts crypto-bot
bash scripts/verify_deploy.sh            # MUST pass before you trust the deploy
```

## 9. Verify the droplet matches the repo

`scripts/verify_deploy.sh [EXPECTED_COMMIT]` is read-only and exits non-zero on
any mismatch. It checks:

1. the checkout is clean and `HEAD` equals `origin/main` (and `EXPECTED_COMMIT`
   when given);
2. the EFFECTIVE value of every win-rate guardrail (`.env` + environment, as the
   app loads it) against the evidence-locked values: `max_open_positions`,
   `aggressive_max_open_positions`, `rollback_max_open_positions` (<= 12),
   `universe_size` (<= 75), `final_pairlist_size` (<= 50), `stop_loss_pct` and
   `atr_stop_min_pct` (>= 0.04), `profitstream_score_threshold` (>= 80),
   `win_rate_position_level` (true), `mean_reversion_exit_enabled` (false), the
   trend/BTC/pullback/breadth gates, and the signal spread cap (the execution order-book spread caps are reported as advisory warnings);
3. `crypto-bot` is active, its unit file equals `deploy/systemd/crypto-bot.service`,
   and the LAST `BOT_REVISION=` line in the journal equals `HEAD` (so the running
   process loaded the code you pulled, not an older commit);
4. `python -m scripts.daily_forensic_report --days 30` runs and prints BOTH the
   position-level (target) and row-level (legacy) win rates.

Win rate is position-level and fee-adjusted. Any historical ~94-95% figure must be
re-validated under that definition before it is used as a target.
