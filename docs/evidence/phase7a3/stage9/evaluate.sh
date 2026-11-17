#!/bin/bash
# protocol.md "Evaluation (run once, as written)", from the frozen worktree; existing outputs are never overwritten
set -u
W="$HOME/Library/Application Support/RoadSense/worktrees/phase7a3-stage9-077b2ee"
PY=${PY:?set PY to the repository venv python}  # e.g. PY=$PWD/venv/bin/python bash evaluate.sh
SCR="$1"
cd "$W" || exit 1
git -C "$W" rev-parse HEAD; git -C "$W" status --porcelain | head -3
evaluate() {
  s=$1; C="$HOME/Library/Application Support/RoadSense/campaigns/phase7a3-s9-$s-20261117"
  O="$HOME/Library/Application Support/RoadSense/analysis/phase7a3-s9-20261117/$s"; mkdir -p "$O"
  T="tools/tracker_replay.py"; export PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1
  python3 - "$C" > "$O/provenance.json" <<'PYEOF'
import json, sys, pathlib
runs = [json.load(open(p)) for p in sorted(pathlib.Path(sys.argv[1]).glob("runs/*/scenario.json"))]
print(json.dumps({"runs": len(runs), "revisions": sorted({r["source_revision"] for r in runs}),
                  "dirty": sorted({r["source_dirty"] for r in runs})}))
PYEOF
  [ -e "$O/verify.json" ] || $PY $T verify "$C" "$O/verify.json" || return 1
  for t in legacy p1 p1p2 oracle; do [ -e "$O/replay-$t" ] || $PY $T replay "$C" "$O/replay-$t" --tracker $t > /dev/null || return 1; done
  for t in legacy p1 p1p2; do [ -e "$O/audit-$t" ] || $PY tools/perception_audit.py "$C" "$O/audit-$t" --slots "$O/replay-$t/slots.jsonl" > /dev/null || return 1; done
  [ -e "$O/criteria.json" ] || $PY $T compare "$O/audit-legacy/perception-audit.json" "$O/audit-p1/perception-audit.json" "$O/audit-p1p2/perception-audit.json" > "$O/criteria.json" || return 1
  R="legacy=$O/replay-legacy p1=$O/replay-p1 p1p2=$O/replay-p1p2"
  [ -e "$O/decision-judgement.json" ] || $PY $T judge "$O/replay-oracle" "$O/decision-judgement.json" legacy="$O/replay-legacy" p1="$O/replay-p1" p1p2="$O/replay-p1p2" > /dev/null || return 1
  [ -e "$O/track-confirmation.json" ] || $PY $T confirm "$C" "$O/track-confirmation.json" legacy="$O/replay-legacy" p1="$O/replay-p1" p1p2="$O/replay-p1p2" > /dev/null || return 1
  for pair in legacy:p1 legacy:p1p2 p1:p1p2; do a=${pair%%:*}; b=${pair##*:}
    [ -e "$O/decision-diff-$a-vs-$b.json" ] || $PY $T diff "$C" "$O/replay-$a" "$O/replay-$b" "$O/decision-diff-$a-vs-$b.json" --oracle "$O/replay-oracle" > /dev/null || return 1; done
  # ran as a scratch draft ($SCR/coverage_draft.py); its output is reproduced exactly by the committed
  # tools/phase7a3_highspeed.py coverage "$C" "$O/replay-legacy/slots.jsonl" "$O/audit-legacy/perception-audit.json"
  [ -e "$O/coverage.json" ] || $PY "$SCR/coverage_draft.py" "$W" "$C" "$O/replay-legacy/slots.jsonl" > "$O/coverage.json" || return 1
  echo "$s done"
}
evaluate diagnostic & evaluate stress & evaluate evaluation & wait
echo "all done"
