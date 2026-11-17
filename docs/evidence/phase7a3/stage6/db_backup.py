"""SQLite-consistent copy of a live history database with the online backup API (the source opened read-only),
then an integrity check of the copy. Usage: db_backup.py SOURCE_DB NEW_COPY (refuses an existing copy)"""
import json
import os
import sqlite3
import sys

source, copy = sys.argv[1], sys.argv[2]
if os.path.exists(copy):
    sys.exit("refusing to overwrite %s" % copy)
src = sqlite3.connect("file:%s?mode=ro" % source, uri=True)
dst = sqlite3.connect(copy)
src.backup(dst)
counts = lambda c: {t: c.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for (t,) in c.execute(
    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()}
result = {"source_counts": counts(src), "copy_counts": counts(dst),
          "copy_integrity_check": dst.execute("PRAGMA integrity_check").fetchone()[0],
          "copy_application_id": dst.execute("PRAGMA application_id").fetchone()[0],
          "copy_user_version": dst.execute("PRAGMA user_version").fetchone()[0]}
dst.close()
src.close()
result["copy_bytes"] = os.path.getsize(copy)
print(json.dumps(result, sort_keys=True))
