"""Read-only look at a history database (run on the Pi): python3 - DIR < pi_inspect.py"""
import json, sys
from roadsense.web import history_export

conn = history_export.connect(sys.argv[1])
q = lambda sql: [dict(r) if hasattr(r, "keys") else r for r in conn.execute(sql).fetchall()]
conn.row_factory = __import__("sqlite3").Row
out = {
    "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
    "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
    "processors": q("SELECT processor_id, started_utc, ended_utc, end_reason, pid FROM processors ORDER BY started_utc"),
    "segments": q("SELECT segment_id, run_id, processing, first_sequence, last_sequence, frames, ended FROM segments "
                  "ORDER BY rowid"),
    "discontinuities": q("SELECT kind, basis, run_id, from_sequence, to_sequence, missing_sequences, "
                         "round(sim_gap_s, 2) sim_gap_s, round(mono_gap_s, 2) mono_gap_s, count FROM discontinuities "
                         "ORDER BY id"),
    "hazard_rows_per_segment": q("SELECT segment_id, count(*) n, count(DISTINCT local_id) hazards FROM hazard_updates "
                                 "GROUP BY segment_id ORDER BY min(id)"),
    "decision_rows_per_segment": q("SELECT segment_id, count(*) n FROM decisions GROUP BY segment_id ORDER BY min(id)"),
    "samples": conn.execute("SELECT count(*) FROM samples").fetchone()[0],
}
print(json.dumps(out, indent=1, default=str))
