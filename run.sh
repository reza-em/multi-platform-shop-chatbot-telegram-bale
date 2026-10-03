#!/bin/bash
# Auto-restart loop. Token is read from the environment (SHOP_TELEGRAM_BOT_TOKEN); never written here.
# Start:  setsid nohup ./run.sh >/dev/null 2>&1 &      Stop: ./stop.sh
cd "$(dirname "$0")"
# single instance: only one run.sh loop at a time
exec 9>run.lock
flock -n 9 || { echo "run.sh already running" >&2; exit 0; }
umask 077
while true; do
  python3 bot.py >> bot.log 2>&1
  echo "$(date '+%F %T') bot exited ($?), restarting in 5s" >> bot.log
  sleep 5
done
