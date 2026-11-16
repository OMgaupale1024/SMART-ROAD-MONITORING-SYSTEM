# Single controlled reboot recovery — PASS, 17 November 2026

The user issued the one approved reboot from the Pi's local console after confirming
physical keyboard/display recovery access. Noninteractive sudo required a password
and logind reported `challenge`, so the agent issued **no reboot commands**, captured
no credentials, and made no service/configuration changes. No additional reboot was
requested or performed by the agent.

Preflight verified SSH, active/enabled RoadSense PID 4899, Linger=yes, working paths,
unit checksum and preserved installation backup. No active file transfer, Git or
package operation was detected. The current working startup and rollback procedure
remain in `docs/operations.md`.

## Recovery verdicts

| Check | Verdict | Evidence |
| --- | --- | --- |
| Actual new boot | PASS | Boot ID changed from f4d9fcde-a31d-4cf9-b50d-dc99bf2ada0d to 662e9af4-4a9c-40a5-a516-3c6defc306d5. |
| Automatic RoadSense startup | PASS | New HTTP server observed before the monitor's first authenticated SSH; no manual Python/service start or desktop interaction by the agent. |
| SSH reconnection | PASS | Existing key authenticated successfully on first post-boot attempt. |
| Service health | PASS | Active, enabled, MainPID 1037, NRestarts=0, no drop-ins; one RoadSense process/listener on port 8001. |
| API health | PASS | GET `/api/live-state`, `/api/system`, `/api/hazards`, `/api/ai-explanation` returned valid responses; live-state correctly returned 404 initially before producer reconnection. |
| Ollama and Qwen3 | PASS | System Ollama active; qwen3:0.6b available; three accepted real explanations in final snapshot. |
| Webots edge processing | PASS | Existing Mac Webots/SUMO stream reconnected automatically, same run ID 8f5013ae9c3b, first observed sequence 3643. Later fresh scene replay produced new run ea8833627b79, advancing sequences, BRAKE and EMERGENCY_BRAKE and four hazard records. |
| Vivaldi | PASS | Isolated temporary profile, LIVE dashboard, valid Next Move/hazard rendering, no horizontal overflow at 1440/1024/820/430 px. Screenshot visually inspected. Browser/profile cleaned up. |
| Safety/AI independence | PASS | RoadSense HTTP became healthy while AI initially reported unavailable/connection refused; recovered without restarting RoadSense when Ollama came up. Prior Phase 5 unreachable/hung tests independently proved brake decisions under AI failure; no safety/source changes in this reboot test. |

The unit remains the exact approved file, SHA256
`f06b38106a390cd6ce6ea7294fb30d784a0fa66c1fbd8edbeede236da54113cf`.
Backup `/home/omgaupale/roadsense-edge-backup-20261117-014533.tar.gz` remains intact,
SHA256 `1f64ea4190907ffef2c8c8effc81edc76fd167093edcd507d6c2f330aebe4903`.
No manual recovery or rollback was required. No reinstall, dependency change,
configuration modification, commit or push was performed.

## Measured recovery times

The local-console command timestamp was unavailable. All network recovery durations
use the **first observed SSH-port outage** as their reference, not an exact
reboot-command timestamp. Polling and request durations affect observation precision.
This measures user-visible interruption including shutdown/firmware/network startup;
it is not equivalent to kernel/userspace boot duration.

| Milestone | Seconds after first observed outage |
| --- | ---: |
| New RoadSense `/api/system` HTTP response | 110.636 |
| SSH TCP port accepts connections | 110.909 |
| Authenticated SSH command completes | 110.957 |
| First fresh edge telemetry (`data_age_s < 2`) | 117.198 |

The watcher checked HTTP before authenticating SSH to avoid mistaking an SSH-triggered
user manager start for boot startup. Recorded first-outage wall timestamp is
1794862797.2843971. `systemd-analyze time` reported **2.659 s kernel + 6.540 s
userspace = 9.200 s**. The user manager reported **459 ms** startup, default.target
at 365 ms. These clocks have different reference points from the observed outage.

The roughly 111-second network interruption warrants attention. Only the current
boot appears in `journalctl --list-boots`, so previous-boot shutdown logs are not
available to explain the remaining delay. No cause is asserted and no logging or
shutdown settings were changed.

Webots console showed automatic recovery with **18 failed publication attempts and
559 snapshots skipped** during the outage. This is expected newest-pending-frame
behavior, not lossless telemetry. This console observation was read before reloading
the world; it is recorded here separately from API counters. Server counters reset
at reboot and therefore cannot describe all frames lost during downtime.

## Post-reboot workload measurements

A 60-second, 120-poll sample includes the already-recovered old run and the beginning
of a fresh hazard replay. A subsequent 15-second sample and final snapshot cover
later hazards/model answers. Sample every 0.5 seconds; raw records retained.

At the final snapshot (server uptime 209.8 s): stream approximately **4.8 Hz**;
926 accepted frames across two runs, zero rejected/missed counters since startup;
fresh-run sequence 515, `processing=edge`. Pipeline mean **0.924 ms**, p95
**2.151 ms**, max **3.490 ms**; handler mean **1.980 ms**, p95 **4.436 ms**,
max **11.575 ms**. These stage distributions cover the current fresh run, not the
reboot outage or end-to-end screen latency.

Real model completed-request latency mean **3520.3 ms**, p95/max **5904.3 ms**.
One output was rejected and two requests cancelled by the normal safety/freshness
policy. An accepted explanation describes the medium pothole in the right lane:
client latency **2879.4 ms**, Ollama total **2873.7 ms**, 40 eval tokens.

During the 60-second sample host CPU ranged **0.2–77.9%** (mean 6.875%), server
RSS **50.2–50.9 MB**, temperature **45.8–54.5 C**. Final RSS 51.0 MB, total RAM
used 32.1%, swap used 0 MB. No throttling observed. Application in-memory state and
hazard history reset at reboot; replay rebuilt four simulated hazard records.

## Evidence and limits

See `reboot-events.json`, `reboot-preflight.json`, `post-reboot.json`,
`post-reboot-sequence.json`, `reboot-boot-analysis.json`, `reboot-ai-check.json`,
`reboot-hazard-replay.json`, `reboot-hazard-followup.json`, `reboot-final-apis.json`,
`reboot-final-service.json`, `reboot-dashboard.json/.png` and
`reboot-vivaldi-*.png`. Text/JSON identifiers are sanitized; screenshots show only
simulated dashboard data and its hostname. SHA256SUMS updated after documentation.

This proves one controlled reboot recovery, not repeated-reboot reliability or a
long-duration soak. Exact console-command-to-recovery times and previous shutdown
delay attribution remain unverified. Cross-reboot access to previous journal entries
was not available. Actual system Ollama shutdown remains the earlier blocked test;
no sudo credentials or configuration changes were requested.

## Closure-review correction

The 18 failed / 559 skipped console figures above are cumulative publisher-lifetime
counters, not verified reboot-only counts. Retain them as observations; do not
report 559 as isolated reboot loss. Early boot/network/SSH timestamps and the
confirmed volatile-journal configuration are analyzed in
[Phase 5 closure](../../phase5-closure.md). Raw historical measurements are unchanged.
