"""Read-only check of a RoadSense history database: integrity, identity, row counts, lifetimes.
Usage: db_check.py DB_PATH  (opened with mode=ro: nothing is written)"""
import json
import sqlite3
import sys

conn = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
print(json.dumps({
    "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
    "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
    "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
    "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
    "counts": {t: conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in tables},
    "processors": [dict(zip(("processor_id", "started_utc", "ended_utc", "end_reason", "pid"), r)) for r in conn.execute(
        "SELECT processor_id, started_utc, ended_utc, end_reason, pid FROM processors ORDER BY started_utc")],
    "discontinuity_kinds": dict(conn.execute("SELECT kind, count(*) FROM discontinuities GROUP BY kind").fetchall()),
}, sort_keys=True))
