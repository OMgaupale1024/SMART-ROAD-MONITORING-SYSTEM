# Phase 5 closure and reboot-outage analysis

Operational recovery is verified; the roughly 110-second interruption has **no proven
root cause**. This review made no remote changes, restart, reboot, deployment, or Git
commit. The installed user service and safety architecture are retained.

## Current working state

Read-only checks on 17 November 2026 found the same boot and MainPID 1037, active and
enabled, NRestarts=0, Restart=on-failure, 3-second restart delay, no drop-ins, and
Linger=yes. One RoadSense process owns port 8001. All four APIs responded; fresh
Webots state reported processing=edge, sequence 1832, age 0.072 seconds at the saved
health snapshot. Ollama was available, qwen3:0.6b enabled. Installed unit and named
backup checksums still match the verified values. See `closure-health.json` under
[Phase 5 evidence](evidence/phase5/). These are observations, not a new acceptance run.

## Two separate clocks and timelines

The external monitor used Mac monotonic time from its first failed SSH-port probe.
It did not record the local-console reboot-command timestamp or every failed probe.
Its loop was sequential: TCP connect (up to 0.25 s), HTTP (up to 4 s), and 0.25 s
sleep. Thus reported readiness means **first observed**, not exact availability.
HTTP observed before TCP in this monitor does not prove HTTP actually started first.

| External milestone | Seconds from first observed SSH-port outage |
| --- | ---: |
| New RoadSense HTTP response | 110.636 |
| SSH TCP success | 110.909 |
| Authenticated SSH command completion observed | 110.957 |
| First fresh edge telemetry | 117.198 |
| HTTP observation to first fresh telemetry | 6.562 |

The Pi journal provides a much better current-boot timeline, on one monotonic clock:

| Pi event | Seconds since current kernel boot |
| --- | ---: |
| Journald starts; runtime journal selected | 3.605 |
| NetworkManager unit active | 6.732 |
| SSH listening on port 22 | 6.809 |
| User manager starts | 7.044 |
| RoadSense user unit start requested | 7.056 |
| RoadSense user unit reported started | 7.086 |
| RoadSense application startup complete | 8.661 |
| Ethernet DHCP lease confirmed | 8.912 |
| Ethernet activated / NetworkManager connected | 8.939–8.940 |
| Ollama system unit reported started | 8.946 |
| Systemd kernel + userspace startup complete | 9.200 |
| First logged successful sensor-frame POST | 15.943 |

A unit's active timestamp is not an HTTP readiness test, and Ollama's active state
is not proof that its generate endpoint/model is ready. Actual journal logs and
later accepted inference evidence supply those distinctions. SSH listened before
RoadSense completed startup, despite the external monitor observing HTTP first.
The application completed startup about 1.60 seconds after its unit start request.
DHCP/connected state preceded the first sensor POST by about **7.00 seconds**;
application startup preceded it by **7.28 seconds**. The existing publisher retries
with exponential backoff capped at 10 seconds, consistent with a post-readiness
retry gap, but exact retry timing was not logged; the gap cannot be fully attributed.

## What is unknown

Early journal wall-minus-monotonic timestamps are consistent to about 2 microseconds
within the extracted window, implying a Pi boot-wall anchor around
1794862898.047829. The monitor's first failed-port wall observation was
1794862797.284397. **If historical Mac/Pi wall clocks were aligned**, that places
about **100.76 seconds before the current kernel boot**. This is a conditional
cross-host inference, not a measured shutdown duration. Current clock probes bound
alignment to roughly milliseconds and NTP is synchronized now; they do not prove
alignment at the historical reboot boundary or bound earlier clock corrections.

The evidence rules out calling the entire 110 seconds current-boot RoadSense
startup, and does not support a 100-second post-boot DHCP wait. It cannot divide the
earlier interval between prior-system shutdown, firmware/power cycle, network path
unavailability, observation delay, or clock effects. Exact console-command time,
previous shutdown completion, power-on/firmware timestamps, and a dense network
probe trace are missing. No root cause is assigned.

The active vendor drop-in `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`
sets **Storage=volatile**. Journald confirmed runtime storage; only the current boot
is listed. ForwardToSyslog=yes is configured, but no active rsyslog/syslog-ng service
or alternate syslog/messages/daemon/kern log files were found. Previous shutdown logs
are therefore unavailable through the checked sources. Merely having `/var/log/journal`
does not override Storage=volatile.

Ollama's configured stop timeout is 90 seconds, while RoadSense's is 10 seconds.
A 90-second stop wait is a **candidate to measure**, not an observed stop wait or
proven cause. Changing either timeout based only on a similar duration is unjustified.

## Snapshot loss: correction and implications

**Correction to the reboot report:** the console showed *18 failed attempts and
559 skipped snapshots*, but `LivePublisher.failed` and `.skipped` accumulate for the
publisher's entire lifetime. They are not reset after a successful reconnect.
Therefore **559 is not a verified reboot-only loss count**, nor is 18 a reboot-only
attempt count. It is a cumulative counter observation at the recovery log.

The earlier console transcript had a recovery showing 4 failed / 37 skipped; the
later one showed 18 / 559. Their differences are 14 / 522 across that broader
interval, which also spans other disruptions. No reboot-boundary counter sample
was preserved, so even this delta cannot isolate reboot-only loss. The raw reboot
JSON/PNG evidence is retained; this review adds the source-based interpretation.

The publisher keeps one pending payload plus at most one in-flight send; a new
payload replaces the waiting one. Its control thread never waits on the network.
The skipped counter counts replacements, not all possible missing sensor observations
or durable data-loss events. Failed HTTP attempts are not necessarily lost payloads.
Server missed-frame counters cover sequence gaps after its first accepted frame
within a run. A reboot resets those counters and starts at the current incoming
sequence, so a first sequence of 3643 does not count the preceding prefix as missed.
Zero post-boot missed counters do not establish lossless delivery through reboot.

In-memory tracking, predictions, hazard map, explanation cache and metrics restart
empty. Source run ID can remain the same across a Pi reboot. Passed hazards may
never be rediscovered. Recovered LIVE means current fresh processing resumed, not
that past data or processor state was restored. Exports and gap records should
identify both producer run and processor lifetime before being interpreted as a
continuous history.

Relevant implementation: `src/roadsense/autonomy/live_publisher.py`,
`src/roadsense/web/metrics.py` (EdgeMetrics.accept), `src/roadsense/web/live.py`, and
`src/roadsense/web/edge.py`. No implementation changes were made.

## Ranked follow-ups (proposals only)

1. **Capture evidence before tuning shutdown.** With separate administrative approval,
   configure bounded persistent journaling on the Pi (explicit disk quota/retention,
   check storage budget), or an existing lightweight durable log destination. Record
   exact reboot request/shutdown timestamps in any separately authorized future test.
   Use independent concurrent SSH/HTTP probes with per-attempt start/end monotonic and
   wall timestamps. Do not install a new logging service or repeat a reboot now.
2. **Add explicit, bounded data-gap records before buffering.** A future small change
   should export producer lifetime/run ID, sequence before/after gap, local send-failure
   and replacement-counter deltas, first/last observation times, cause known/unknown,
   and processor-instance identity/reset. Detect reboot gaps across processor lifetimes
   without pretending the missing prefix was processed. Bound retention; batch/persist
   outside the control/safety loop. Avoid claiming exact sensor loss from skip counters.
3. **Consider a bounded local archive only if historical reconstruction is required.**
   Keep the newest-only live path. A separate bounded Mac sensor ring/spool could retain
   raw frames with byte/time caps and explicit eviction counts. At 5 Hz, a two-minute
   window contains about 600 frames; actual byte cost needs representative raw-frame
   measurements. Any disk write or catch-up work must stay off the control loop.
   Replay only into an explicitly historical/offline path with ordering/deduplication
   and producer/processor identities. Never drain old frames into the live safety API
   after reconnection: arrival-based freshness could make old decisions appear LIVE,
   and the sequence validator rejects backfilled frames after newer ones. Do not add
   a buffer unless bounded retention requirements justify its extra failure modes.
4. **Tune only the measured bottleneck.** If future shutdown evidence proves Ollama or
   another unit exhausts its stop timeout, review a targeted change with a rollback
   plan and approval. Do not couple RoadSense startup to Ollama readiness, reduce
   timeouts blindly, or change deterministic safety algorithms.

## Closure

Phase 5 service cutover, crash recovery, graceful stop/start, unreachable/hung AI,
Webots stale/reconnect handling, and one real reboot recovery are validated. The
outage cause remains an explicitly open diagnostic issue. Actual privileged Ollama
shutdown, repeated-reboot reliability and long soak remain untested. Existing
acceptance/reboot raw evidence is preserved, with checksum manifests extended for
closure evidence. No new downtime or Vivaldi session was needed for this read-only
review. See [commit proposal](phase5-commit-plan.md) for the exact proposed scope.
