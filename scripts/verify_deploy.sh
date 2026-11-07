#!/usr/bin/env bash
# Read-only droplet parity check. Exits non-zero if the droplet does not match
# the repo. Run AFTER `git pull --ff-only origin main` + bootstrap_droplet.sh:
#
#   bash scripts/verify_deploy.sh [EXPECTED_COMMIT]
#
# EXPECTED_COMMIT defaults to origin/main. See docs/DEPLOY_DIGITALOCEAN.md §9.
set -uo pipefail

APP_DIR=${APP_DIR:-/opt/crypto-trading-machine}
SERVICE=${SERVICE:-crypto-bot}
cd "$APP_DIR" || { echo "FAIL: cannot cd to $APP_DIR"; exit 1; }
PY=${PY:-$APP_DIR/.venv/bin/python}

FAILS=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }

echo "== 1. git state =="
git fetch -q origin main || fail "git fetch origin main failed"
HEAD_SHA=$(git rev-parse HEAD)
ORIGIN_SHA=$(git rev-parse origin/main)
EXPECTED=${1:-$ORIGIN_SHA}
echo "HEAD=$HEAD_SHA  origin/main=$ORIGIN_SHA  expected=$EXPECTED"
[[ "$HEAD_SHA" == "$EXPECTED"* || "$HEAD_SHA" == "$EXPECTED" ]] \
  && pass "HEAD matches expected commit" || fail "HEAD != expected (run: git pull --ff-only origin main)"
[[ "$HEAD_SHA" == "$ORIGIN_SHA" ]] \
  && pass "HEAD == origin/main" || fail "HEAD != origin/main"
if [[ -z "$(git status --porcelain)" ]]; then pass "working tree clean"; else
  fail "working tree has local changes:"; git status --short; fi

echo; echo "== 2. effective guardrail settings (.env + environment, as the app loads them) =="
"$PY" -m scripts.verify_deploy_settings && pass "all win-rate guardrails hold" \
  || fail "a guardrail is widened or settings failed to load"

echo; echo "== 3. service + running revision =="
if systemctl is-active --quiet "$SERVICE"; then pass "$SERVICE is active"; else fail "$SERVICE is not active"; fi
UNIT_SRC="$APP_DIR/deploy/systemd/$SERVICE.service"
UNIT_DST="/etc/systemd/system/$SERVICE.service"
if diff -q "$UNIT_SRC" "$UNIT_DST" >/dev/null 2>&1; then pass "installed unit == repo unit"; else
  fail "installed unit differs from $UNIT_SRC (re-run scripts/bootstrap_droplet.sh)"; fi
# Only read the journal since the service last (re)started: BOT_REVISION is logged
# once per start, and scanning the whole journal can take minutes on a long-lived box.
SINCE=$(systemctl show "$SERVICE" -p ActiveEnterTimestamp --value 2>/dev/null)
RUN_REV=$(timeout 60 journalctl -u "$SERVICE" --since "${SINCE:-1 hour ago}" --no-pager -o cat 2>/dev/null | grep -o 'BOT_REVISION=[0-9a-f]\{7,40\}' | tail -1 | cut -d= -f2)
echo "journal BOT_REVISION=${RUN_REV:-<none>}"
if [[ -n "$RUN_REV" && "$RUN_REV" == "$HEAD_SHA" ]]; then pass "running process loaded HEAD"; else
  fail "running revision (${RUN_REV:-none}) != HEAD — restart: systemctl restart $SERVICE"; fi
systemctl status "$SERVICE" --no-pager -l | sed -n 1,12p

echo; echo "== 4. win-rate report (must print BOTH definitions) =="
OUT=$("$PY" -m scripts.daily_forensic_report --days 30 --mode live 2>&1); RC=$?
echo "$OUT" | sed -n 1,14p
if [[ $RC -eq 0 ]] && grep -q "POSITION-level" <<<"$OUT" && grep -q "ROW-level" <<<"$OUT"; then
  pass "forensic report ran and printed position-level + row-level win rates"
elif grep -q "no DB at" <<<"$OUT"; then
  echo "NOTE  no trading.db yet (fresh droplet) — nothing to report"
else
  fail "forensic report failed or is missing a win-rate definition"
fi

echo
if [[ $FAILS -eq 0 ]]; then echo "DEPLOY VERIFIED: droplet matches $HEAD_SHA"; exit 0; fi
echo "DEPLOY NOT VERIFIED: $FAILS check(s) failed"; exit 1
