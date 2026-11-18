#!/bin/zsh
# W10 / section 7 run matrix (docs/webots-sensor-correction.md): one scenario (normal, seed 1, highway_lht), 60 s
# simulated per run, one isolated run directory each. Runs already present in <out dir> are skipped.
#
#   tools/webots_w8/matrix.sh <out dir> [<patched AB webots executable, from tools/webots_w8/build.sh>]
#
# Then: PYTHONPATH=src venv/bin/python tools/sensor_profile.py analyse <out dir>/* --out runs.json
#       venv/bin/python tools/webots_w8/tables.py runs.json
set -euo pipefail
O=${1:?out dir}; AB=${2:-}
cd ${0:A:h}/../..
mkdir -p $O
run() { local name=$1; shift; [[ -d $O/$name ]] && return 0
        PYTHONPATH=src venv/bin/python tools/sensor_profile.py run --out $O/$name "$@" > $O/$name.out 2>&1; echo "done $name"; }
for cfg in "A 200 200" "B 200 100" "C 100 100" "D 50 50"; do
  set -- ${=cfg}
  for sync in stock aligned smooth; do run $1-$sync --sumo-ms $2 --radar-ms $3 --sync $sync; done
done
run C-current --sumo-ms 100 --radar-ms 100 --sync current
run original-baseline --sumo-ms 200 --radar-ms 200 --sync stock --stock-models
run C-aligned-nolidar --sumo-ms 100 --radar-ms 100 --sync aligned --lidar-ms 0
run C-aligned-camera --sumo-ms 100 --radar-ms 100 --sync aligned --camera-s 0.1 --duration 20
if [[ -n $AB ]]; then
  run C-aligned-AB --sumo-ms 100 --radar-ms 100 --sync aligned --webots $AB
  run C-smooth-AB --sumo-ms 100 --radar-ms 100 --sync smooth --webots $AB
fi
