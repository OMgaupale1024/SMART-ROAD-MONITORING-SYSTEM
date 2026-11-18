#!/bin/zsh
# W6: highway scratch copy, SUMO step x radar period; 60 s simulated each, fast mode, no rendering.
S=${1:?usage: w6_run.sh <scratch dir holding copy/, a copy of simulation/ with the W6 probe patch>}
W=$S/copy/simulation/webots/worlds/roadsense_highway.wbt
for pair in 200:200 200:50 100:100 50:50; do
  sumo=${pair%%:*}; radar=${pair##*:}; out=$S/v_${sumo}_${radar}; rm -rf $out; mkdir -p $out
  python3 - "$W" "$sumo" <<'PY'
import re, sys
p, step = sys.argv[1], sys.argv[2]
s = open(p).read()
s = re.sub(r"SumoInterface \{\n  gui FALSE\n(  step \d+\n)?", "SumoInterface {\n  gui FALSE\n  step %s\n" % step, s, count=1)
open(p, "w").write(s)
PY
  grep -A3 "^SumoInterface" $W | tr '\n' ' ' > $out/sumo_block.txt
  t0=$(python3 -c 'import time;print(time.time())')
  (cd $S/copy && W6_PROBE=$out/probe.jsonl W6_RADAR_PERIOD_MS=$radar W6_STOP_S=60 /usr/bin/time -l /Applications/Webots.app/Contents/MacOS/webots --batch --stdout --stderr --mode=fast --no-rendering $W > $out/console.txt 2>&1) &
  bg=$!
  while kill -0 $bg 2>/dev/null; do
    if grep -q "W6 PROBE DONE" $out/console.txt 2>/dev/null; then
      python3 -c 'import time;print(time.time())' > $out/t_done.txt
      pkill -TERM -P $bg -f "Webots.app/Contents/MacOS/webots --batch" 2>/dev/null
      pgrep -f "$S/copy" | xargs kill -TERM 2>/dev/null
      break
    fi
    sleep 0.2
  done
  wait $bg 2>/dev/null
  echo "$t0" > $out/t_start.txt
  echo "done $pair"
done
