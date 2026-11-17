# Persistent history and the ML dataset foundation (`roadsense.history.v1`)

RoadSense keeps its live state in memory: a restart empties the hazard map, the tracks and the
metrics. The history store keeps a durable record of what the edge processor decided and found:
hazard updates, safety decisions, gaps in the stream, processor lifetimes, and (opt-in) dataset
samples for later model training. It is an **observer**. The safety pipeline never reads it, never
waits for it, and publishes the same decisions with it on or off (tested).

Code: `src/roadsense/web/history.py` (recorder), `src/roadsense/web/history_export.py` (read-only
summary and export). Tests: `tests/test_history.py`. Benchmark: `tools/bench_history.py`.

**Off by default.** Nothing is written unless `ROADSENSE_HISTORY_ENABLE=1`. It has not been
deployed to the Raspberry Pi yet (Phase 6B, see [the plan below](#raspberry-pi-deployment-phase-6b-proposed)).

## Architecture

```text
POST /api/sensor-frame ─▶ validate ─▶ Pipeline.process() ─▶ snapshot ─▶ WebSocket clients
                                                               │
                                     LiveStateHub listeners ───┼──▶ AIExplainer.observe()    (Ollama worker)
                                                               └──▶ HistoryRecorder.observe()
                                                                      │ queue.put_nowait(reference)   ~30 µs at 5 Hz
                                                                      ▼
                                                     bounded queue (256 states, ~4 MB worst case)
                                                                      │
                                                     RoadSenseHistory thread: the only connection
                                                       reduce states to events ─▶ batch ─▶ SQLite (WAL)
                                                       retention, size limit, free-space check
                                                                      │
                                              python -m roadsense.web.history_export (separate process,
                                              read-only): summary, dataset export
```

- `observe()` runs on the event loop after the snapshot has gone to the clients. It puts a
  *reference* to the published state on a `queue.Queue` with `put_nowait`: no copy, no JSON, no
  disk. A published state is never mutated afterwards (every snapshot is built from new dicts;
  `live_state.finished()` copies), which is what makes the reference safe.
- If the queue is full, or the store is disabled, failed or closed, the state is **dropped and
  counted**. `observe()` cannot block and cannot raise; the hub additionally isolates listeners.
- One daemon thread owns the only database connection. It reduces states to events, batches them
  (at most 2 s, 500 rows), commits each batch in one `BEGIN IMMEDIATE` transaction, and runs
  retention once a minute. Nothing in the event loop touches SQLite.
- `GET /api/system` gains a `history` block from in-memory counters only (no database access),
  so it answers while the store is failing.
- No new HTTP route. Historical reads go through a command-line tool in its own process with a
  read-only connection, so a long query can never delay sensor ingestion. `GET /api/hazards` is
  unchanged and still means **the live map**.

## Configuration

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `ROADSENSE_HISTORY_ENABLE` | off | `1` records history |
| `ROADSENSE_HISTORY_DIR` | `~/.local/state/roadsense/history` (Linux, `XDG_STATE_HOME` honoured); `~/Library/Application Support/RoadSense/history` (macOS) | must be absolute: a relative path would land in the source checkout the service runs in |
| `ROADSENSE_HISTORY_RETENTION_DAYS` | 30 | rows older than this are deleted |
| `ROADSENSE_HISTORY_MAX_MB` | 256 | limit on SQLite pages in use (see [disk space](#retention-and-disk-space)) |
| `ROADSENSE_HISTORY_MIN_FREE_MB` | 512 | below this much free space on the filesystem, writing pauses |
| `ROADSENSE_HISTORY_DATASET` | off | `1` also records one dataset sample per `ROADSENSE_HISTORY_SAMPLE_S` |
| `ROADSENSE_HISTORY_SAMPLE_S` | 0.2 | simulation seconds between samples (0.2 = every frame at 5 Hz) |
| `ROADSENSE_HISTORY_QUEUE` | 256 | states waiting for the writer (51 s at 5 Hz) |
| `ROADSENSE_HISTORY_FLUSH_S` | 2.0 | longest time an event waits in memory before its transaction |

An unusable value is logged and the default used, as for `ROADSENSE_AI_*`. The database file is
`roadsense-history.sqlite3` in the directory. A `roadsense-history.sqlite3.lock` file holds an
exclusive `flock` while a server writes, so a second server on the same directory reports `in_use`
instead of interleaving lifetimes. The lock is released by the kernel if the process dies.

## Identity model

| Identity | Form | Meaning |
| -------- | ---- | ------- |
| producer run | `run_id` (from Webots) | one simulation run; survives a server restart |
| processor lifetime | `processor_id`, 12 hex, new at every server start | one RoadSense process |
| segment | `<processor_id>-<nnnn>` | one producer run as one processor saw it, without interruption, with one kind of processing (`edge` or `precomputed`) |
| local hazard id | `PH_001` (from the hazard map) | unique within one pipeline only |
| historical hazard | `<segment_id>/<local_id>` | one hazard of one pipeline instance |
| hazard event | `(segment_id, local_id, sequence)` | one stored update of it |
| decision / sample event | `(segment_id, sequence)` | one stored decision / sample |

A **new segment starts exactly when `LiveStateHub` starts a new pipeline** (new processor, new
`run_id`, or a switch between edge and precomputed input), which is exactly when local hazard ids
restart. Therefore `PH_001` of two lifetimes of one run, or of run A before and after run B, are
two historical hazards. They are never merged or overwritten. Whether they are the same pothole
is a question for a future map: this store does not answer it.

**Deduplication:** event keys are `UNIQUE` and written with `INSERT OR IGNORE`. A genuine
duplicate (same segment, same sequence) is ignored; within a segment the hub already refuses
repeated sequence numbers (409). Different observations cannot share a key, because the sequence
is strictly increasing within a segment. Discontinuity rows are generated once by the single
writer and carry no natural key.

## Database schema

`PRAGMA application_id = 0x52534831` ("RSH1") marks the file as a RoadSense history;
`PRAGMA user_version = 1` is the schema version; `meta` holds `schema`, `created_utc`, `created_by`.

| Table | One row per | Key columns |
| ----- | ----------- | ----------- |
| `processors` | server lifetime | `processor_id`, `started_utc`, `last_commit_utc`, `ended_utc`, `end_reason` (`clean`, `not_observed`, or null while running), host, version, pid, `counters` (JSON at a clean end) |
| `segments` | segment | `run_id`, `processing`, first/last `sequence`, first/last `sim_s`, first/last `utc`, `frames`, `ended` (end-of-run marker seen) |
| `hazard_updates` | material hazard change | `segment_id`, `local_id`, `sequence`, `sim_s`, `utc`, `change`, type, severity, confidence, observations, status, `frame` (`webots_world`), `x_m`, `y_m`, first/last seen (simulation s), dimensions, sensor, vehicle, `simulated` |
| `decisions` | decision change | `segment_id`, `sequence`, `sim_s`, `utc`, `trigger`, risk, action, target speed, lane, reason, primary-threat type/id, its distance, longitudinal/lateral position, relative velocity, TTC or time to hazard, minimum TTC, conflicts, relevant hazards, ego speed and lane |
| `discontinuities` | lifecycle event or gap | `kind`, `basis`, `run_id`, from/to sequence, `missing_sequences`, `sim_gap_s`, `mono_gap_s`, `count`, `detail` (JSON) |
| `samples` | dataset sample | `segment_id`, `sequence`, `sim_s`, `utc`, `feature_version`, `features` (JSON), `labels` (JSON) |

Every event table has an index on `utc` (retention) and its unique key; all SQL is parameterized;
table names in SQL text are constants. There is no geographic column: positions are Webots metres
in the `webots_world` frame and every hazard row says `simulated = 1`.

**Version policy:** an empty file becomes version 1. A file with another `application_id` or
`user_version`, or one SQLite cannot read (`SQLITE_NOTADB`, `SQLITE_CORRUPT`), is reported as
`incompatible`, never retried and never modified; the server runs on without history. To start a
new history, stop the server and move the file aside. There is no automatic migration: a future
version 2 must add one explicitly.

## Event policy

| Event | Written when |
| ----- | ------------ |
| hazard `created` | a local id is first seen in a segment |
| hazard `severity`, `status` | it changes |
| hazard `confidence` | it moved ≥ 0.05 since the hazard's last row |
| hazard `position` | the merged position moved ≥ 0.5 m since the last row |
| hazard `settled` | its observation count changed and it has not been observed for 5 s of simulation time (it was passed) |
| hazard `final` | at the end of a segment (end marker, run change, clean shutdown), if anything changed since the last row |
| hazard `resync` | the first row after rows were lost (see failures) |
| decision `first` | the first decision of a segment |
| decision `change` | risk, action, lane or primary threat changed, or the target speed moved ≥ 10 km/h |
| decision `final` / `resync` | as for hazards |
| sample | every `ROADSENSE_HISTORY_SAMPLE_S` of simulation time, dataset mode only |

On the 60 s benchmark episode this is about 20 decision rows and 12 hazard rows per minute instead
of 300 snapshots. Every frame is still visible in `segments` (first/last sequence, frame count).
Ollama explanations are **not** stored: they are wording, not decisions, and never labels.

## Clocks, gaps and lifecycle records

Three clocks are kept apart and never compared with one another: simulation time (`sim_s`,
`sim_gap_s`, hazard `first_seen_s`), wall-clock UTC (`utc`, when the processor observed the state),
and the processor's monotonic clock (`mono_gap_s`, stalls only).

| `kind` | `basis` | Meaning |
| ------ | ------- | ------- |
| `processor_start` | observed | this lifetime began; `detail` names the previous lifetime and how it ended |
| `processor_end_not_observed` | inferred | written by the next lifetime for one that never recorded a clean end. Its `last_commit_utc` bounds what was saved; how much was processed after it is **unknown** |
| `run_observed` | observed | first segment of a `run_id` in this database |
| `run_change` | observed | this processor switched from another run (or processing kind) |
| `run_resumed` | inferred | the run was last seen by another lifetime: `from_sequence` is that lifetime's **last committed** sequence, so `missing_sequences` is an upper bound for this processor's view, not a frame count |
| `run_reentered` | observed | the same, within one lifetime (run A, then B, then A) |
| `sequence_gap` | observed | sequence numbers this processor never accepted, between two states of a segment: producer skips (its newest-only publisher), refused frames (400/409/413), or network loss. Which one is not known; it is **not** a count of lost sensor observations |
| `stream_stall` | measured | ≥ 2 s on the processor's clock between two states (paused Webots, network outage); `sim_gap_s` shows how much simulation time passed meanwhile |
| `run_ended` | observed | the producer's end-of-run marker |
| `history_loss` | measured | states the history layer itself dropped (`detail`: `queue_full`, `unavailable`, `disk_low`, `extract_error`, `shutdown`), counted exactly; these are subtracted from the neighbouring `sequence_gap` |
| `history_resumed` | measured | the store recovered after a failure; `count` is the rows of failed batches |

The `roadsense.sensors.v1` frame carries no producer counters (the Webots publisher's skipped-frame
total stays in the Webots console), so none are recorded or inferred.

## Failures

The store fails toward "fewer rows", never toward a slower or failed frame.

| Failure | What happens | Recovery |
| ------- | ------------ | -------- |
| queue full | state dropped in `observe()`, `lost.queue_full` counted, `history_loss` row later | automatic |
| invalid directory, permission, read-only file or filesystem | `state: unavailable`, `last_error.kind` `cannot_open` / `read_only`; queued states drained and counted `unavailable` | retried 1 s, 2 s, … capped at 60 s; one log line per minute per kind |
| database locked > 1 s | the whole batch rolls back (`batches.failed`, `lost.uncommitted_rows`), connection reopened | as above; the next state rewrites every hazard and the decision (`resync`) |
| disk full (`SQLITE_FULL`) | as above, kind `disk_full` | as above; free space or a lower `MAX_MB` |
| free space below `MIN_FREE_MB` | `state: paused`, states counted `disk_low` | resumes when space returns (checked every 10 s) |
| corrupt or incompatible file | `state: incompatible`, file left untouched, `observe()` stops queueing (`lost.inactive`) | an operator moves the file aside and restarts |
| unexpected exception in the writer | recorded as `kind: error`, connection reset | as above |
| writer thread killed | `state: stopped`, `observe()` stops queueing (`lost.inactive`) | server restart |
| unreadable state | skipped, `lost.extract_error` | per state |
| shutdown | queue drained for up to `close_timeout` (3 s), then a final batch, `end_reason = clean`; anything left is counted `shutdown` | bounded: the systemd unit allows 10 s |
| process killed (SIGKILL, OOM, power) | the queue and the unwritten batch are lost; SQLite rolls back an incomplete transaction | the next lifetime records `processor_end_not_observed`; `PRAGMA integrity_check` stays `ok` (tested with SIGKILL) |

Faults in the tests are real SQLite or filesystem errors where that is safe: a held write lock,
a `chmod`-read-only file and directory, a path under a regular file, garbage bytes, a newer
`user_version`, a foreign SQLite file, SQLite's own `max_page_count` for a genuine `SQLITE_FULL`
(no disk was filled), and a SIGKILLed process. Injected: a slow database open (to fill the queue),
an exception from the writer, and a killed writer thread. A real full disk, a real read-only
remount and power loss were **not** tested.

**Durability limits.** `synchronous = NORMAL` in WAL mode: a committed batch survives a process
crash; a power cut can lose the last commits but cannot corrupt the database. What is queued (up to
256 states) or batched (up to 2 s) when the process dies is lost, and only the next lifetime's
`processor_end_not_observed` row says so. **This is not a zero-loss store.**

## Health in `GET /api/system`

```json
"history": {
  "schema": "roadsense.history.status.v1", "enabled": true, "state": "recording", "reason": null,
  "processor_id": "3f2a9c1d7e4b", "dataset": false, "degraded": false, "losses_observed": false,
  "queue": {"size": 0, "capacity": 256, "high_water": 2},
  "states": {"observed": 1500, "absorbed": 1500},
  "lost": {"queue_full": 0, "inactive": 0, "unavailable": 0, "extract_error": 0, "shutdown": 0,
           "uncommitted_rows": 0},
  "rows": {"hazard_updates": 61, "decisions": 102, "discontinuities": 3, "samples": 0},
  "batches": {"committed": 141, "failed": 0, "latency_ms": {"last": 0.4, "mean": 0.5, "p50": 0.4, "p95": 0.9, "max": 3.1}},
  "retention": {"deleted_by_age": 0, "deleted_by_size": 0, "retention_days": 30.0, "limit_mb": 256.0, "min_free_mb": 512.0},
  "storage": {"db_bytes": 4096, "wal_bytes": 412032, "used_bytes": 151552, "free_page_bytes": 0,
              "filesystem_free_bytes": 104190566400, "measured_utc": 1794900000.0},
  "last_error": null
}
```

`state` is `disabled`, `starting`, `recording`, `unavailable`, `paused`, `incompatible` or
`stopped`. `degraded` means "not recording now"; `losses_observed` means "this lifetime's history
has holes". Counters reset with the process, like the edge counters. The database path is not in
the answer (it names the home directory); the startup log line prints it.

## Retention and disk space

What is enforced, once a minute in the writer thread, in transactions of at most 2,000 rows and
20 transactions per pass:

1. **Age:** event rows with `utc` older than `RETENTION_DAYS` are deleted, then segments and ended
   processor lifetimes that have nothing left.
2. **Size:** while the SQLite pages in use (`page_count − freelist_count`) exceed `MAX_MB`, the
   oldest rows go, samples first (they are the bulk and can be recorded again by rerunning a
   scenario), then decisions, hazard updates and discontinuities. Each pass deletes down to 90 %
   of the limit.
3. **Free space:** below `MIN_FREE_MB` writing pauses.

What can temporarily exceed the target:

- **The file does not shrink.** Deleted rows become free pages that SQLite reuses; there is no
  `VACUUM` (it would rewrite the whole file and block the writer). The file stays at its
  high-water size, about `MAX_MB` plus what arrives between two maintenance passes.
- **The WAL file** grows by every commit until a checkpoint (automatic every ~4 MB of WAL, in the
  writer thread, `PASSIVE`) and is truncated to 4 MB after one. A reader that keeps a transaction
  open stops checkpoints from going past it, so the WAL can grow while it reads. The export tool
  therefore reads in pages of 5,000 rows, each its own short transaction.
- **Exports** are separate files and count against the filesystem, not against `MAX_MB`.
- **Retention cost** is bounded per pass (40,000 rows at most); a backlog after a long outage is
  worked off over several minutes.

The Raspberry Pi's root filesystem is a 113 GB ext4 USB disk with 97 GB free (read-only check,
17 November 2026; `/dev/sda2`, not an SD card). The 256 MB default is about 0.3 % of that.

## Dataset export

```sh
PYTHONPATH=src python3 -m roadsense.web.history_export summary
PYTHONPATH=src python3 -m roadsense.web.history_export export ~/roadsense-dataset-20261117 \
    [--format jsonl|csv] [--split 70,15,15] [--seed roadsense] [--group-map groups.json]
```

`--dir` selects another history directory (default: `ROADSENSE_HISTORY_DIR`, then the platform
default). The output directory must be new or empty: an export never overwrites. The same database
gives byte-identical files (every query has a total order and the manifest has no timestamp).

| File | Contents |
| ---- | -------- |
| `manifest.json` | `roadsense.dataset.v1`: source schema, feature version and definitions (name, unit, meaning), label definitions and provenance, data provenance, split parameters and the group → split assignment, every file's row count and SHA-256, warnings |
| `samples.jsonl` / `samples.csv` | one sample per line: split, group, run, segment, processor, processing, sequence, simulation time, recorded UTC, `simulated: true`, features, labels (CSV: flattened, `label_` prefix, empty cell = null) |
| `decisions.jsonl`, `discontinuities.jsonl`, `segments.jsonl`, `processors.jsonl` | the tables, with run/group/split added where they belong to a segment |
| `hazard_updates.jsonl`, `hazards.jsonl` | every hazard update, and the latest one per historical hazard, in the `roadsense.hazard.v1` vocabulary with `historical_id`; `position.geo` is always `null` |

### Features (`roadsense.features.v1`, frozen)

Every feature comes from the state of the sample's own frame, so no later information reaches a
row. 51 columns: ego speed, acceleration (null on a run's first frame), heading, steering, lane,
lane change in progress, whether left/right driving lanes exist, track count; four vehicle slots
(`lead` and `follower` in the ego lane, nearest `left` and `right`), each with presence,
longitudinal/lateral position, relative velocity, radar range rate, track age and time since last
measured; the nearest ACTIVE hazard ahead in the ego lane (presence, distance, severity 0–2,
confidence) and the hazards ahead within 100 m; two **rule-derived** features (`derived_min_ttc_s`,
`derived_conflicts`, which a model can use to copy the rule: drop them to learn something else);
and gap indicators (`sequence_gap_before`, `history_dropped_before`, `sim_dt_s`, null for a
segment's first sample). Unavailable values are null, never zero: a `*_present = 0` slot has null
fields. The manifest lists every column with its unit.

### Labels are heuristic

`overall_risk`, `recommended_action`, `recommended_speed_kmh`, `recommended_lane`,
`primary_threat_type` are the deterministic rule's output for the same frame
(`label_provenance.kind = "heuristic"`, `ground_truth = false`). They are not observed collision
outcomes. A model trained on them learns to imitate `safety.decide()`. Evaluating a model will
need independent outcomes (scenario ground truth from the SUMO supervisor, recorded separately).
Predicting a *future* risk needs labels built from later rows at training time, grouped by segment
so that no window crosses a gap.

Event history alone is not a training set. BRAKE and EMERGENCY_BRAKE rows are rare transitions:
a representative corpus also needs ordinary driving and safe interactions with their temporal
context, which is what dataset mode records (every frame by default). Raw sensor frames are not
stored.

### Splits

Splits are by **group**, never by frame: adjacent frames are nearly identical, so a frame-level
split leaks. A group is the `run_id`, or what `--group-map` (a JSON object `run_id → group`) says.
Its split is a hash of the seed and the group, so it does not depend on which other groups exist
and adding runs never moves old ones. Runs of the same world with SUMO `--seed=1` are near-identical
scenarios, so a run-level split can still leak between them. The manifest warns about this unless
a group map is given. The sensor frame carries no scenario or seed identity, so the map has to come
from the operator.

## Performance

Measured on the development Mac (Apple silicon, Python 3.12.14, SQLite 3.53.4) with
`tools/bench_history.py`, on the synthetic workload described in its docstring (repeating 60 s
episode: lead-car braking to EMERGENCY_BRAKE, overtaking traffic, potholes every 250 m). These are
**not Raspberry Pi results**.

Evidence: [`docs/evidence/phase6a/`](evidence/phase6a/) (raw JSON; `cpu_growth.py` produced the
CPU table). All runs record **dataset mode** unless stated, the worst case for the store. Ollama
was off in these runs, to isolate the store.

### Real server over HTTP, 5 frames/s, 300 s each (1,501 frames), history off then on

| Measure | Off | On (dataset) |
| ------- | --- | ------------ |
| HTTP status of every POST | 1,501 × 200 | 1,501 × 200 |
| `handler` p50 / p95 / max (server, last 1,500 frames) | 0.879 / 1.553 / 3.19 ms | 0.914 / 1.579 / 14.77 ms |
| `process` p50 / p95 / max | 0.398 / 0.676 / 2.46 ms | 0.409 / 0.792 / 2.10 ms |
| POST round trip, client side: mean / p95 / max | 2.61 / 3.94 / 5.35 ms | 3.10 / 4.16 / 17.77 ms |
| `GET /api/live-state` ping at 10 Hz (event-loop responsiveness): p50 / p95 / max | 1.556 / 2.572 / 6.18 ms (2,886 pings) | 1.561 / 2.712 / 10.29 ms (2,884) |
| server RSS first / max | 49.8 / 50.9 MB | 50.5 / 54.1 MB |
| queue high-water / states lost | n/a | 1 of 256 / 0 |
| batches: committed, failed, p95 / max | n/a | 150, 0, 1.51 / 3.80 ms |
| graceful shutdown | 0.227 s | 0.128 s |

Handler p95 rises by 0.03 ms and mean by 0.05 ms. The client-side round trip rises more (mean
+0.49 ms): each `put` wakes the writer, whose work then competes for Python's GIL while the event
loop sends the HTTP response. The WebSocket clients already have the state by then (listeners run
after publication), and the ping shows the event loop otherwise unaffected. One 14.8 ms handler
outlier in the "on" run has no determined cause (GC, scheduling or a checkpoint are all possible).
All of this is under 1 % of a 200 ms cycle.

### Same frames in one process, 5 frames/s, 300 s each

| Measure | Result |
| ------- | ------ |
| published states identical, off vs on (all fields but `generated_at_unix_s`) | **yes**, all 1,501: 1,063 MAINTAIN, 236 BRAKE, 60 EMERGENCY_BRAKE, 121 MONITOR, 21 SLOW_DOWN |
| `ingest_sensors()` mean / p95, off vs on | 0.95 / 1.62 ms vs 1.05 / 1.67 ms |
| `observe()` alone: p50 / p95 / max | 29 / 52 / 446 µs (1 µs at max rate: at 5 Hz the cost is waking the writer thread) |
| heap held by one published state | 15.6 kB, so a full 256-state queue holds about 4 MB |
| rows written | 1,500 samples, 118 decisions, 78 hazard updates, 2 discontinuities; 0 lost |

### CPU, three repeats of the 300 s scenario at full speed

| Mode | CPU per frame (whole process) | Overhead | One core at 5 Hz |
| ---- | ----------------------------- | -------- | ---------------- |
| history off | 130–137 µs | | |
| events only (the proposed default) | 187–189 µs | +53–58 µs | 0.03 % |
| dataset | 203–207 µs | +70–74 µs | 0.04 % |

The Pi 5 is slower per core than this Mac; even ten times these figures is under 0.5 % of a core.
The writer cost per frame grows with the number of hazards in the map, as the pipeline's does:
the hazard map keeps every hazard of a run (a one-hour synthetic run with 316 potholes cost both
several times more). The Webots road has four.

### Growth and storage requirements

| Mode | Measured | Per hour of driving at 5 Hz | Fills 256 MB in |
| ---- | -------- | --------------------------- | --------------- |
| events only | 116 kB per 300 s (198 rows) | about 1.4 MB | about 180 h |
| dataset | 2.24 MB per 300 s (identical in the server and in-process runs) | about 27 MB | about 9.5 h |

The WAL reached 4.1 MB during the run (its 4 MB truncation target, plus one transaction) and was
removed at the clean close. Rows per hour depend on the scenario: the benchmark brakes once a
minute and passes a pothole every 11 s. A busier randomized scenario writes more decision rows;
samples (one per frame) are unaffected by how eventful the drive is.

Examples: 100 randomized runs of 5 minutes in dataset mode are about 112 MB; a month of 2 hours a
day of events-only history is about 85 MB, under the default limit and retention.

## Raspberry Pi deployment (Phase 6B, proposed)

Not performed; needs approval. The running service, its unit and the backup stay as they are
until then.

1. **Read-only preflight:** `systemctl --user status roadsense-edge`, MainPID, one listener on 8001,
   the existing backup's checksum, `df -h ~`, `ls -ld ~/.local/state`.
2. **New backup** of `~/roadsense-edge/src` as a new timestamped archive with its SHA-256 (the
   existing `roadsense-edge-backup-20261117-014533.tar.gz` is not touched).
3. **Copy three files:** `src/roadsense/web/history.py`, `src/roadsense/web/history_export.py`,
   `src/roadsense/web/server.py` (and `tools/bench_history.py` for the on-Pi benchmark).
4. **Restart with history still off**, the unit unchanged: `systemctl --user restart roadsense-edge`.
   Verify one listener, the four APIs, `history.state = "disabled"`, BRAKE/EMERGENCY_BRAKE from a
   Webots replay. This proves the new code without the new behaviour.
5. **Enable** with a user drop-in, leaving the unit file itself unchanged:
   `~/.config/systemd/user/roadsense-edge.service.d/history.conf` containing
   `[Service]` / `Environment=ROADSENSE_HISTORY_ENABLE=1`; `systemctl --user daemon-reload`;
   restart. Verify `state = "recording"` and the database under `~/.local/state/roadsense/history/`.
6. **Acceptance** as below; dataset mode only afterwards, as a separate step
   (`Environment=ROADSENSE_HISTORY_DATASET=1` in the same drop-in).

### Hardware acceptance thresholds (proposed)

Each measured in one session, against a baseline taken with history off on the same Pi, the same
Webots scene, Ollama on, for at least 30 minutes each:

| Measure | Threshold | Why |
| ------- | --------- | --- |
| `process` p95 | within 15 % of the baseline's, and ≤ 2.6 ms | history adds nothing to that stage; 15 % covers the run-to-run spread already seen (2.026 / 2.262 / 2.151 ms) |
| `handler` p95 | ≤ baseline + 0.5 ms | the listener is the only addition; 0.5 ms is about 100× its measured Mac cost and still 0.25 % of a 200 ms cycle |
| telemetry rate | ≥ 4.7 Hz | baseline 4.8 Hz |
| `lost.*` | all 0; `queue.high_water` ≤ 25 | no loss in normal operation; a queue that is used at all means the writer is lagging |
| `batches.latency_ms` p95 / max | ≤ 50 ms / ≤ 500 ms | a USB disk commit; 500 ms is far inside the queue's 51 s of slack |
| server RSS | ≤ baseline + 15 MB | queue worst case ~4 MB, page cache 2 MB, the rest headroom on a 4 GB Pi |
| host CPU mean | ≤ baseline + 3 points | the writer is idle most of the time |
| temperature / throttling | no throttling flags | as in Phase 4 and 5 |
| restart | `systemctl --user restart` gives `processor_start` + `run_resumed` (inferred) and both lifetimes `clean` | lifecycle records |
| crash | `SIGKILL` of MainPID: next lifetime writes `processor_end_not_observed`; `PRAGMA integrity_check` `ok` | durability claim |
| growth | measured bytes per hour within 2× of the Mac figures above | storage planning |
| export | `history_export summary` and `export` on the Pi, files and checksums consistent | read-only access |

A reboot test, if wanted, needs its own approval, as in Phase 5.

### Rollback

- **Turn history off:** remove the drop-in, `systemctl --user daemon-reload`, restart. The database
  stays where it is and is never read by the safety path.
- **Remove the code:** restore the three files from the new backup and restart; the old
  `server.py` simply does not import the history module.
- The database directory can be moved aside or deleted at any time with the server stopped. Neither
  rollback touches Ollama, the unit file, the existing backup or lingering.

## Fit with the planned randomized Indian-road simulation and ML training

What the foundation already gives such a pipeline:

- **Dataset generation needs no Pi.** Run the server on the Mac with `ROADSENSE_HISTORY_ENABLE=1`,
  `ROADSENSE_HISTORY_DATASET=1`, a dedicated `ROADSENSE_HISTORY_DIR` per campaign and a larger
  `ROADSENSE_HISTORY_MAX_MB`, then drive Webots in fast mode (about 50 frames/s). The writer kept up
  with about 480 states/s in dataset mode in the full-speed run, so fast mode leaves it idle most of
  the time.
- **Every randomized run is its own `run_id`**, and each run is a group for splitting. Scenario
  families (same map, same randomization recipe, same seed) go into `--group-map`, so related runs
  never straddle train and test.
- **The storage schema does not depend on the features.** Features and labels are JSON with a
  `feature_version`; a `roadsense.features.v2` for new road types is a new extractor, not a
  database migration. Hazard rows already carry a free `type` (the hazard map numbers non-pothole
  types `HZ_001`, so a speed breaker would fit).
- **Simulation time is the join key** for outcomes recorded elsewhere: `(run_id, sim_s)` lines up a
  sample with whatever the SUMO supervisor logs as ground truth.

What it does **not** yet cover, and would need when that work starts:

- **Scenario identity.** `roadsense.sensors.v1` forbids extra fields, so the seed and parameters of
  a randomized run cannot travel with the frames. Simplest: the scenario generator writes a
  `run_id → family, seed, parameters` file, used as `--group-map` and kept beside the export.
- **Ground-truth outcomes** (collisions, near misses, pothole impacts) as labels independent of
  the rule. Not recorded anywhere yet.
- **Indian-road features.** `features.v1` assumes this world's straight three-lane carriageway, lane
  relations and vehicle-only tracks. Unmarked lanes, two-wheelers and auto-rickshaws, and speed
  breakers need new perception outputs first, then `features.v2`.
- **Sequence windows.** Samples are per frame; windows for sequence models are built at training
  time, per segment, with `sequence_gap_before` and `sim_dt_s` marking where a window must not cross.

## Known limits

- Not lossless (see durability). The queue and the open batch are lost on a crash or power cut.
- The size limit governs pages in use, not the file: the file keeps its high-water size and the WAL
  can exceed 4 MB while a long reader is open.
- No historical API over the network and no dashboard view of history: the command-line tool is
  the only reader. A future read-only route would need paging and a worker thread.
- All data is simulated: frame `webots_world`, `geo` null, `simulated` true. Real GNSS needs new
  fields, a new producer and a schema version 2.
- Labels are heuristic, the scenario family is not recorded, and nothing is trained here.
- Counters reset with the process; only the database outlives it.
- The existing hazard map never expires a hazard within a run, so per-frame cost (pipeline and
  history alike) grows with very long runs; irrelevant on the 5 km Webots road, relevant to long
  randomized drives.
