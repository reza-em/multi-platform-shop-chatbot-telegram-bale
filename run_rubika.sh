#!/bin/bash
# Rubika bot: auto-restart loop (separate process, own log rubika.log, own state state_rubika.json).
# Token from env SHOP_RUBIKA_BOT_TOKEN (never written anywhere).  Start: setsid nohup ./run_rubika.sh >/dev/null 2>&1 &
cd "$(dirname "$0")"
exec 9>run_rubika.lock
flock -n 9 || { echo "run_rubika.sh already running" >&2; exit 0; }
umask 077
export SHOP_PLATFORM=rubika
while true; do
  python3 bot.py >> rubika.log 2>&1
  echo "$(date '+%F %T') rubika bot exited ($?), restarting in 5s" >> rubika.log
  sleep 5
done
