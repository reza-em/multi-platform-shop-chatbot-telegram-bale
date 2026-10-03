#!/bin/bash
# Stops ONLY the Bale loop + process of this project (matched by script name + working dir).
D="$(cd "$(dirname "$0")" && pwd)"
for p in $(pgrep -x -f "/bin/bash ./run_rubika.sh"); do [ "$(readlink /proc/$p/cwd)" = "$D" ] && kill "$p"; done
for p in $(pgrep -x -f "python3 bot.py"); do
  [ "$(readlink /proc/$p/cwd)" = "$D" ] && tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -qx "SHOP_PLATFORM=rubika" && kill "$p"
done
