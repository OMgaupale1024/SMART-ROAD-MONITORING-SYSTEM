#!/bin/zsh
# Phase 7A.3 Stage 12: traced replays (normal and geometry-consistent) and failure analyses of every recording set.
set -e
cd "$(dirname "$0")/../../../.."
C="$HOME/Library/Application Support/RoadSense/campaigns"
A="$HOME/Library/Application Support/RoadSense/analysis"
O="$A/phase7a3-s12-20261117"
G="$O/geometry.json"
typeset -A CAMP ORACLE
CAMP=(stage1 phase7a3-recording-20261117 diagnostic phase7a3-s9-diagnostic-20261117 evaluation phase7a3-s9-evaluation-20261117 stress phase7a3-s9-stress-20261117)
ORACLE=(stage1 "$A/phase7a3-20261117/replay-oracle" diagnostic "$A/phase7a3-s9-20261117/diagnostic/replay-oracle" evaluation "$A/phase7a3-s9-20261117/evaluation/replay-oracle" stress "$A/phase7a3-s9-20261117/stress/replay-oracle")
for set in ${=SETS:-stage1 diagnostic evaluation stress}; do
  for tracker in ${=TRACKERS:-legacy p1 p1p2}; do
    for mode in ${=MODES:-normal consistent}; do
      name="trace-$tracker"; extra=()
      [[ $mode == consistent ]] && { name="trace-$tracker-consistent"; extra=(--consistent "$G"); }
      out="$O/$set/$name"
      [[ -d "$out" ]] || PYTHONPATH=src venv/bin/python tools/track_failures.py trace "$C/${CAMP[$set]}" "$out" --tracker $tracker $extra >/dev/null
      res="$O/$set/failures-${name#trace-}.json"
      [[ -f "$res" ]] || PYTHONPATH=src venv/bin/python tools/track_failures.py analyse "$C/${CAMP[$set]}" "$out" "${ORACLE[$set]}" "$res" >/dev/null
      echo "$(date +%T) $set $name done"
    done
  done
done
