"""Read-only access to a RoadSense history database: a summary, and the versioned dataset export (docs/history.md).

    python -m roadsense.web.history_export summary [--dir DIR]
    python -m roadsense.web.history_export export OUT_DIR [--dir DIR] [--format jsonl|csv]
           [--split 70,15,15] [--seed SEED] [--group-map FILE]

The database is opened read-only, in its own process, so nothing here can delay the edge server; reads go in pages
of PAGE rows, each its own short read transaction, so a long export does not hold back the writer's checkpoints.
The same database gives the same files, byte for byte: every query has a total order and the manifest has no clock.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence
from urllib.parse import quote

from roadsense.web import history

DATASET_SCHEMA = "roadsense.dataset.v1"
PAGE = 5000
SPLITS = ("train", "validation", "test")


def connect(directory: Path) -> sqlite3.Connection:
    """The history database in directory, read-only; raises history.Incompatible or sqlite3.Error."""
    path = Path(directory) / history.DB_NAME
    if not path.is_file():
        raise FileNotFoundError("no history database at %s" % path)

    def read_only(name, **kwargs):
        return sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(name)), uri=True, **kwargs)
    conn = history.open_database(path, read_only, create=False)
    conn.row_factory = sqlite3.Row
    return conn


def _pages(conn, table: str, keys: Sequence[str], columns: str = "*") -> Iterator[sqlite3.Row]:
    """Every row of table in the order of its unique keys, PAGE at a time (keyset pagination)."""
    order = ", ".join(keys)
    last = None
    while True:
        where = "" if last is None else "WHERE (%s) > (%s)" % (order, ", ".join("?" * len(keys)))
        rows = conn.execute("SELECT %s FROM %s %s ORDER BY %s LIMIT %d" % (columns, table, where, order, PAGE),
                            last or ()).fetchall()
        yield from rows
        if len(rows) < PAGE:
            return
        last = tuple(rows[-1][key] for key in keys)


def summary(conn) -> Dict[str, Any]:
    counts = {table: conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
              for table in ("processors", "segments") + history.TABLES}
    pages = conn.execute("PRAGMA page_count").fetchone()[0] - conn.execute("PRAGMA freelist_count").fetchone()[0]
    return {
        "schema": history.SCHEMA,
        "rows": counts,
        "used_bytes": pages * conn.execute("PRAGMA page_size").fetchone()[0],
        "processors": [dict(r) for r in conn.execute(
            "SELECT processor_id, started_utc, last_commit_utc, ended_utc, end_reason, version FROM processors "
            "ORDER BY started_utc DESC, processor_id LIMIT 20")],
        "segments": [dict(r) for r in conn.execute(
            "SELECT segment_id, run_id, processing, first_sequence, last_sequence, first_sim_s, last_sim_s, frames, "
            "ended FROM segments ORDER BY last_utc DESC, segment_id LIMIT 20")],
        "discontinuities": {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, count(*) AS n FROM discontinuities GROUP BY kind ORDER BY kind")},
    }


def assign(group: str, seed: str, fractions: Sequence[float]) -> str:
    """The split of a group: a hash of seed and group, so it never depends on which other groups exist."""
    digest = hashlib.sha256(("%s\0%s" % (seed, group)).encode("utf-8")).digest()
    position, total = int.from_bytes(digest[:8], "big") / 2.0 ** 64, 0.0
    for name, fraction in zip(SPLITS, fractions):
        total += fraction
        if position < total:
            return name
    return SPLITS[-1]


def _hazard_record(row: sqlite3.Row, segment: Dict[str, Any]) -> Dict[str, Any]:
    """A hazard update in the roadsense.hazard.v1 vocabulary, with its historical identity."""
    return {
        "historical_id": "%s/%s" % (row["segment_id"], row["local_id"]),
        "local_id": row["local_id"], "segment_id": row["segment_id"], "sequence": row["sequence"],
        "change": row["change"], "type": row["type"], "severity": row["severity"], "confidence": row["confidence"],
        "observations": row["observations"], "status": row["status"],
        "dimensions_m": None if row["dimensions"] is None else {
            k[:-2]: v for k, v in json.loads(row["dimensions"]).items()},
        "position": {"frame": row["frame"], "x_m": row["x_m"], "y_m": row["y_m"], "geo": None},
        "observed": {"clock": "simulation", "sim_s": row["sim_s"], "first_seen_s": row["first_seen_s"],
                     "last_seen_s": row["last_seen_s"], "recorded_utc": row["utc"]},
        "provenance": {"simulated": bool(row["simulated"]), "sensor": row["sensor"], "vehicle_id": row["vehicle_id"],
                       "run_id": segment["run_id"], "processor_id": segment["processor_id"],
                       "processing": segment["processing"]},
    }


class _Writer:
    """One output file, hashed and counted as it is written."""

    def __init__(self, directory: Path, name: str):
        self.name, self.rows = name, 0
        self._file = open(directory / name, "w", encoding="utf-8", newline="")
        self._hash = hashlib.sha256()

    def write(self, text: str) -> None:
        self._file.write(text)
        self._hash.update(text.encode("utf-8"))

    def line(self, record: Dict[str, Any]) -> None:
        self.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        self.rows += 1

    def close(self) -> Dict[str, Any]:
        self._file.close()
        return {"rows": self.rows, "sha256": self._hash.hexdigest()}


def export(conn, out: Path, fmt: str = "jsonl", fractions: Sequence[float] = (0.7, 0.15, 0.15),
           seed: str = "roadsense", group_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Write the dataset files and manifest.json into out (a new or empty directory); returns the manifest."""
    if fmt not in ("jsonl", "csv"):
        raise ValueError("format must be jsonl or csv")
    if len(fractions) != 3 or any(f < 0 for f in fractions) or abs(sum(fractions) - 1.0) > 1e-9:
        raise ValueError("split must be three non-negative fractions that sum to 1")
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError("%s is not empty: an export never overwrites files" % out)
    out.mkdir(parents=True, exist_ok=True)
    segments = {r["segment_id"]: dict(r) for r in conn.execute("SELECT * FROM segments ORDER BY segment_id")}
    groups = {run: (group_map or {}).get(run, run) for run in sorted({s["run_id"] for s in segments.values()})}
    split_of = {group: assign(group, seed, fractions) for group in sorted(set(groups.values()))}
    files: Dict[str, Dict[str, Any]] = {}

    def table(name, rows):
        w = _Writer(out, name)
        for record in rows:
            w.line(record)
        files[name] = w.close()

    def context(segment_id):
        s = segments.get(segment_id) or {}
        run = s.get("run_id")
        return {"run_id": run, "group": groups.get(run), "split": split_of.get(groups.get(run)),
                "processor_id": s.get("processor_id"), "processing": s.get("processing")}

    table("processors.jsonl", (dict(r, counters=r["counters"] and json.loads(r["counters"])) for r in conn.execute(
        "SELECT * FROM processors ORDER BY started_utc, processor_id")))
    table("segments.jsonl", (dict(s, ended=bool(s["ended"]), **{k: v for k, v in context(sid).items()
                                                               if k in ("group", "split")})
                             for sid, s in segments.items()))
    table("discontinuities.jsonl", (dict(r, detail=r["detail"] and json.loads(r["detail"]))
                                    for r in _pages(conn, "discontinuities", ("id",))))
    table("decisions.jsonl", (dict({k: r[k] for k in r.keys() if k != "id"}, **context(r["segment_id"]))
                              for r in _pages(conn, "decisions", ("segment_id", "sequence"))))
    latest: Dict[str, Dict[str, Any]] = {}  # one per historical hazard: the map at the end of what was recorded

    def updates():
        for r in _pages(conn, "hazard_updates", ("segment_id", "local_id", "sequence")):
            record = _hazard_record(r, segments.get(r["segment_id"]) or {})
            latest[record["historical_id"]] = record
            yield record
    table("hazard_updates.jsonl", updates())
    table("hazards.jsonl", (latest[k] for k in sorted(latest)))

    names = [name for name, _, _ in history.FEATURE_COLUMNS]
    label_names = [name for name, _ in history.LABEL_COLUMNS]
    per_split = {name: 0 for name in SPLITS}
    versions = set()
    sample_file = "samples." + fmt
    w = _Writer(out, sample_file)
    meta = ("split", "group", "run_id", "segment_id", "processor_id", "processing", "sequence", "sim_time_s",
            "recorded_utc", "simulated", "feature_version")
    rows_out = None
    if fmt == "csv":
        class _Sink:
            def write(self, text):
                w.write(text)
        rows_out = csv.writer(_Sink(), lineterminator="\n")
        rows_out.writerow(meta + tuple(names) + tuple("label_" + n for n in label_names))
    for r in _pages(conn, "samples", ("segment_id", "sequence")):
        c = context(r["segment_id"])
        versions.add(r["feature_version"])
        per_split[c["split"]] = per_split.get(c["split"], 0) + 1
        row = {"split": c["split"], "group": c["group"], "run_id": c["run_id"], "segment_id": r["segment_id"],
               "processor_id": c["processor_id"], "processing": c["processing"], "sequence": r["sequence"],
               "sim_time_s": r["sim_s"], "recorded_utc": r["utc"], "simulated": True,
               "feature_version": r["feature_version"]}
        f, lab = json.loads(r["features"]), json.loads(r["labels"])
        if rows_out is None:
            w.line(dict(row, features=f, labels=lab))
        else:
            rows_out.writerow([row[k] for k in meta] + ["" if f.get(n) is None else f[n] for n in names]
                              + ["" if lab.get(n) is None else lab[n] for n in label_names])
            w.rows += 1
    files[sample_file] = w.close()

    warnings: List[str] = []
    if not files[sample_file]["rows"]:
        warnings.append("no samples: the history was recorded without ROADSENSE_HISTORY_DATASET=1, so there is "
                        "event history but no dataset")
    if versions - {history.FEATURES}:
        warnings.append("samples of feature versions %s are not described by this manifest"
                        % sorted(versions - {history.FEATURES}))
    empty = [name for name, f in zip(SPLITS, fractions) if f > 0 and not any(v == name for v in split_of.values())]
    if empty and split_of:
        warnings.append("split %s has no group: %d groups are too few for these fractions"
                        % (", ".join(empty), len(split_of)))
    if not group_map:
        warnings.append("grouped by run_id only: runs of one SUMO seed and world are near-identical scenarios, so a "
                        "run-level split can still leak; give --group-map to group runs by scenario family")
    manifest = {
        "schema": DATASET_SCHEMA,
        "source": {"history_schema": history.SCHEMA, "schema_version": history.SCHEMA_VERSION},
        "feature_version": history.FEATURES,
        "features": [{"name": n, "unit": u, "description": d} for n, u, d in history.FEATURE_COLUMNS],
        "labels": [{"name": n, "description": d} for n, d in history.LABEL_COLUMNS],
        "label_provenance": {
            "source": history.LABEL_SOURCE, "kind": "heuristic", "ground_truth": False,
            "note": "the deterministic safety rule's output for the same frame, not an observed collision outcome; "
                    "evaluating a model needs independent outcomes (scenario truth) recorded separately"},
        "data": {"simulated": True, "producer": "webots", "coordinate_frame": history.FRAME, "geo": None,
                 "note": "positions are Webots world metres; there are no geographic coordinates"},
        "split": {"unit": "group", "group_by": "group_map" if group_map else "run_id", "seed": seed,
                  "fractions": dict(zip(SPLITS, fractions)), "assignment": split_of,
                  "groups_per_split": {n: sum(1 for v in split_of.values() if v == n) for n in SPLITS},
                  "samples_per_split": per_split},
        "files": dict(sorted(files.items())),
        "warnings": warnings,
    }
    with open(out / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m roadsense.web.history_export",
                                     description="Read a RoadSense history database (read-only).")
    parser.add_argument("--dir", type=Path, default=None,
                        help="history directory (default: ROADSENSE_HISTORY_DIR, else %s)" % history.default_directory())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("summary", help="row counts, recent processor lifetimes and segments, discontinuities")
    e = commands.add_parser("export", help="the dataset files and manifest.json, into a new or empty directory")
    e.add_argument("out", type=Path)
    e.add_argument("--format", choices=("jsonl", "csv"), default="jsonl", help="of the samples file")
    e.add_argument("--split", default="70,15,15", help="train,validation,test percentages (default 70,15,15)")
    e.add_argument("--seed", default="roadsense", help="split seed (default roadsense)")
    e.add_argument("--group-map", type=Path, help="JSON object run_id -> group: runs of a group share a split")
    args = parser.parse_args(argv)
    directory = args.dir or Path(history.config_from_environment(log=lambda _: None).directory
                                 or history.default_directory())
    try:
        conn = connect(directory)
        if args.command == "summary":
            json.dump(summary(conn), sys.stdout, indent=2)
            print()
            return 0
        fractions = [float(v) / 100.0 for v in args.split.split(",")]
        group_map = None
        if args.group_map:
            group_map = json.loads(args.group_map.read_text(encoding="utf-8"))
            if not isinstance(group_map, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                          for k, v in group_map.items()):
                raise ValueError("--group-map must be a JSON object of run_id strings to group strings")
        manifest = export(conn, args.out, args.format, fractions, args.seed, group_map)
        print(json.dumps({"out": str(args.out), "files": manifest["files"], "warnings": manifest["warnings"]},
                         indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, history.Incompatible) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
