#!/bin/bash
# Phase 7A.3 Stage 14: configuration D (P1+P2+P4, "p1p2p4") and the legacy+P4 ablation ("legacyp4") on every
# recorded set, beside A/B/C (Stages 8-12). Offline replays of the recorded sensor frames only; truth evaluates.
# Existing outputs are never overwritten. Usage: PY=$PWD/venv/bin/python [OUT=dir] bash docs/evidence/phase7a3/stage14/run.sh
set -u
PY=${PY:?set PY to the repository venv python}
R="$HOME/Library/Application Support/RoadSense"; C="$R/campaigns"; A="$R/analysis"
O="$A/${OUT:-phase7a3-s14-20261118}"; S12="$A/phase7a3-s12-20261117"  # OUT: P4 v1 s14, v2 s14b
T=tools/tracker_replay.py; F=tools/track_failures.py
export PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1
campaign() { case $1 in stage1) echo "$C/phase7a3-recording-20261117";; *) echo "$C/phase7a3-s9-$1-20261117";; esac; }
earlier() { case $1 in stage1) echo "$A/phase7a3-20261117";; *) echo "$A/phase7a3-s9-20261117/$1";; esac; }
run_set() {
  s=$1; CA=$(campaign "$s"); E=$(earlier "$s"); D="$O/$s"; mkdir -p "$D"
  # 1. the default path is unchanged: A, B and C replayed from this source reproduce Stages 8-9 byte for byte
  for t in legacy p1 p1p2; do
    [ -e "$D/regression-$t" ] || $PY $T replay "$CA" "$D/regression-$t" --tracker $t > /dev/null || return 1
    for f in slots.jsonl decisions.jsonl; do
      cmp -s "$D/regression-$t/$f" "$E/replay-$t/$f" || { echo "$s $t $f DIFFERS"; return 1; }; done
    cmp -s <(gzip -dc "$D/regression-$t/tracks.jsonl.gz") <(gzip -dc "$E/replay-$t/tracks.jsonl.gz") \
      || { echo "$s $t tracks DIFFER"; return 1; }
  done
  echo "$s regression identical"
  # 2. D and legacy+P4, traced (a trace writes replay's slots, decisions and tracks, plus every cycle)
  for t in p1p2p4 legacyp4; do
    [ -e "$D/trace-$t" ] || $PY $F trace "$CA" "$D/trace-$t" --tracker $t > /dev/null || return 1
    [ -e "$D/audit-$t" ] || $PY tools/perception_audit.py "$CA" "$D/audit-$t" --slots "$D/trace-$t/slots.jsonl" > /dev/null || return 1
    [ -e "$D/failures-$t.json" ] || $PY $F analyse "$CA" "$D/trace-$t" "$E/replay-oracle" "$D/failures-$t.json" > /dev/null || return 1
    [ -e "$D/failures-$t-body.json" ] || $PY $F analyse "$CA" "$D/trace-$t" "$S12/$s/replay-oracle-body" "$D/failures-$t-body.json" > /dev/null || return 1
    [ -e "$D/expiries-$t.json" ] || $PY $F expiries "$CA" "$D/expiries-$t.json" --tracker $t > /dev/null || return 1
  done
  [ -e "$D/criteria.json" ] || $PY $T compare "$E/audit-legacy/perception-audit.json" "$E/audit-p1/perception-audit.json" \
    "$E/audit-p1p2/perception-audit.json" "$D/audit-p1p2p4/perception-audit.json" "$D/audit-legacyp4/perception-audit.json" > "$D/criteria.json" || return 1
  RS=(legacy="$E/replay-legacy" p1="$E/replay-p1" p1p2="$E/replay-p1p2" p1p2p4="$D/trace-p1p2p4" legacyp4="$D/trace-legacyp4")
  [ -e "$D/decision-judgement.json" ] || $PY $T judge "$E/replay-oracle" "$D/decision-judgement.json" "${RS[@]}" > /dev/null || return 1
  [ -e "$D/decision-judgement-body.json" ] || $PY $T judge "$S12/$s/replay-oracle-body" "$D/decision-judgement-body.json" "${RS[@]}" > /dev/null || return 1
  [ -e "$D/track-confirmation.json" ] || $PY $T confirm "$CA" "$D/track-confirmation.json" "${RS[@]}" > /dev/null || return 1
  [ -e "$D/decision-diff-p1p2-vs-p1p2p4.json" ] || $PY $T diff "$CA" "$E/replay-p1p2" "$D/trace-p1p2p4" "$D/decision-diff-p1p2-vs-p1p2p4.json" --oracle "$E/replay-oracle" > /dev/null || return 1
  [ -e "$D/decision-diff-legacy-vs-legacyp4.json" ] || $PY $T diff "$CA" "$E/replay-legacy" "$D/trace-legacyp4" "$D/decision-diff-legacy-vs-legacyp4.json" --oracle "$E/replay-oracle" > /dev/null || return 1
  echo "$s done"
}
for s in ${SETS:-stage1 diagnostic evaluation stress}; do run_set "$s" & done
wait
# a trace's outputs are replay's: one direct replay of D checked against its trace
E=$O/evaluation
[ -e "$E/replay-p1p2p4" ] || $PY $T replay "$(campaign evaluation)" "$E/replay-p1p2p4" --tracker p1p2p4 > /dev/null
for f in slots.jsonl decisions.jsonl; do cmp -s "$E/replay-p1p2p4/$f" "$E/trace-p1p2p4/$f" && echo "evaluation D $f: replay = trace"; done
echo "all done"
