#!/bin/bash
# Stops ONLY the Telegram loop + bot.py of this project (Bale/Rubika have their own stop_*.sh).
D="$(cd "$(dirname "$0")" && pwd)"
for p in $(pgrep -x -f "/bin/bash ./run.sh"); do [ "$(readlink /proc/$p/cwd)" = "$D" ] && kill "$p"; done
for p in $(pgrep -x -f "python3 bot.py"); do
  [ "$(readlink /proc/$p/cwd)" = "$D" ] || continue
  tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -qx "SHOP_PLATFORM=bale" && continue
  tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -qx "SHOP_PLATFORM=rubika" && continue
  kill "$p"
done
