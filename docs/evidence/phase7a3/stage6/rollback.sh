#!/bin/sh
# RoadSense Phase 7A.3 Stage 6 rollback: put back the Phase 6B history.py (eb4b1c4) and restart roadsense-edge once.
# The database needs no rollback: the fix changes neither its schema nor how rows are written. To return to the
# pre-deployment copy anyway: stop the service, move ~/.local/state/roadsense/history aside, create it again with
# db-backup/roadsense-history.sqlite3 from this directory in it, start the service.
set -eu
W="$HOME/roadsense-history-fix-7a3-20261117"
TARGET="$HOME/roadsense-edge/src/roadsense/web/history.py"
OLD=9e9e2d80897875ecbb00f6522ac7238df185ce11fdfb4cda72712c22e756830c
echo "$OLD  $W/history.py.eb4b1c4" | sha256sum -c -
cp "$W/history.py.eb4b1c4" "$TARGET"
echo "$OLD  $TARGET" | sha256sum -c -
systemctl --user restart roadsense-edge
i=0
until curl -sf -m 2 http://127.0.0.1:8001/api/system > /dev/null; do
    i=$((i + 1)); [ "$i" -lt 30 ] || { echo "service not answering"; exit 1; }; sleep 1
done
systemctl --user is-active roadsense-edge
