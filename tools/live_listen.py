"""Dev/test tool — print the RoadSense live-state stream, one line per snapshot (no dashboard needed).

    python tools/live_listen.py                        # ws://127.0.0.1:8000/ws/live
    python tools/live_listen.py --json > states.jsonl  # every snapshot, one JSON object per line
    python tools/live_listen.py ws://127.0.0.1:9000/ws/live

Start the web server (python -m roadsense.web) and the Webots world with ROADSENSE_LIVE_PUBLISH=1 first; see
docs/live-state.md. Needs the web extra (its websockets package). Ctrl+C stops it.
"""
from __future__ import annotations

import argparse
import json
import sys

from websockets.sync.client import connect


def summary(state: dict) -> str:
    """One line: the cycle, the car, the tracks and hazards around it, and the unified recommendation."""
    ego, decision = state["ego"], state["unified_safety"] or {}
    tracks = sorted(state["tracks"], key=lambda t: t["distance_m"])
    conflicts = [t for t in tracks if t["collision"] and t["collision"]["conflict"]]
    ahead = sorted((h for h in state["hazards"] if h["relative"]["direction"] == "ahead"),
                   key=lambda h: h["relative"]["distance_m"])
    threat = decision.get("primary_threat") or {}
    return ("seq=%d t=%.1fs speed=%.1fkm/h tracks=%d nearest=%s conflicts=%d hazards=%d next_hazard=%s "
            "risk=%s action=%s target=%skm/h threat=%s" % (
                state["simulation"]["sequence"], state["timestamp"], ego["speed_kmh"], len(tracks),
                "%s@%.1fm" % (tracks[0]["track_id"], tracks[0]["distance_m"]) if tracks else "none",
                len(conflicts), len(state["hazards"]),
                "%s@%.1fm" % (ahead[0]["hazard_id"], ahead[0]["relative"]["distance_m"]) if ahead else "none",
                decision.get("overall_risk"), decision.get("recommended_action"),
                decision.get("recommended_speed_kmh"), threat.get("id", "none"))
            + ("" if state["simulation"]["active"] else " (simulation ended)"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/live_listen.py", description=__doc__.split("\n")[0])
    parser.add_argument("url", nargs="?", default="ws://127.0.0.1:8000/ws/live")
    parser.add_argument("--json", action="store_true", help="print each snapshot as received, one per line")
    args = parser.parse_args(argv)
    try:
        with connect(args.url, max_size=None) as ws:
            print("connected to %s" % args.url, file=sys.stderr, flush=True)
            for message in ws:
                print(message if args.json else summary(json.loads(message)), flush=True)
    except KeyboardInterrupt:
        return 0
    except OSError as error:
        print("cannot connect to %s: %s (is python -m roadsense.web running?)" % (args.url, error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
