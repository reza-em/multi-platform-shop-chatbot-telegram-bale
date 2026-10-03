#!/bin/bash
# Bale bot: auto-restart loop (separate process, own log bale.log, own state state_bale.json).
# Token from env SHOP_BALE_BOT_TOKEN (never written anywhere).  Start: setsid nohup ./run_bale.sh >/dev/null 2>&1 &
cd "$(dirname "$0")"
exec 9>run_bale.lock
flock -n 9 || { echo "run_bale.sh already running" >&2; exit 0; }
umask 077
export SHOP_PLATFORM=bale
while true; do
  python3 bot.py >> bale.log 2>&1
  echo "$(date '+%F %T') bale bot exited ($?), restarting in 5s" >> bale.log
  sleep 5
done
