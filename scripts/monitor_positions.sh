#!/bin/bash
# Monitor positions held by the trading bot
# Extracts position info from autopilot logs

TARGET=6
LOGFILE="/workspaces/crypto-trading-machine/logs/uvicorn.log.1"

echo "=================================================="
echo "POSITION MONITOR - Target: $TARGET positions"
echo "=================================================="

while true; do
  TIMESTAMP=$(date -Iseconds)
  
  # Extract [HELD] lines from recent logs (shows current positions)
  POSITIONS=$(grep "\[HELD\]" "$LOGFILE" 2>/dev/null | tail -10 | awk '{print $2}' | sed 's/USDT.*//' | sort -u)
  
  COUNT=$(echo "$POSITIONS" | grep -c "^[A-Z]" 2>/dev/null || echo 0)
  
  # Format output
  if [ "$COUNT" -eq "$TARGET" ]; then
    STATUS="✅ OK"
  else
    STATUS="⚠️  LOW ($COUNT/$TARGET)"
  fi
  
  echo ""
  echo "[$TIMESTAMP] Positions: $COUNT/$TARGET $STATUS"
  
  if [ $COUNT -gt 0 ]; then
    echo "  Held: $POSITIONS"
  else
    echo "  Held: (none or awaiting first tick)"
  fi
  
  # Check for risk brakes
  LOSS_STREAK=$(grep "loss_streak_pause" "$LOGFILE" 2>/dev/null | tail -1)
  if [ -n "$LOSS_STREAK" ]; then
    echo "  ⚠️  Risk Manager: $(echo "$LOSS_STREAK" | grep -o 'streak=[0-9]*' | head -1)"
  fi
  
  sleep 30
done
