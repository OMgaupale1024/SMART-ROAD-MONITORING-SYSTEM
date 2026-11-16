# Phase 5 operational reliability — measured 17 November 2026

This report records the initial service/fault test stage. The subsequently approved
single reboot test is complete: see [reboot recovery results](REBOOT_RESULTS.md).

## Final configuration

Installed the approved `deploy/roadsense-edge.user.service` verbatim as
`~/.config/systemd/user/roadsense-edge.service`. It is **active and enabled**;
final MainPID **4899** owns the only port-8001 listener. `Restart=on-failure`,
restart delay 3 seconds, stop timeout 10 seconds, and preexisting `Linger=yes`
were verified. Installed-unit SHA256:
`f06b38106a390cd6ce6ea7294fb30d784a0fa66c1fbd8edbeede236da54113cf`.

No sudo, reboot, dependency changes, application-source changes, safety-algorithm
changes, commits or pushes occurred. Python 3.13.5 retains checkout-first src and
the existing direct-Python dependency path. Real Ollama qwen3:0.6b is available,
AI explanations enabled and normal timeout restored to 20 seconds. `DropInPaths`
is empty; the owned hung stub service is inactive, and its temporary files were
removed. The isolated Vivaldi browser/profile were closed and removed.

## Recovery results

| Test | Measurement and outcome |
| --- | --- |
| Manual → user service | Gracefully stopped reverified PID 2849, installed/validated/enabled the user unit; one listener, HTTP healthy. Manual rollback was not needed. |
| Application crash | SIGKILL sent only to MainPID; NRestarts rose 0→1, PID changed 3763→4251. HTTP recovered in **4.059 s** measured from before the SSH fault command. First polled live state followed **3.503 s** later (about **7.562 s** overall, 0.5 s polling resolution plus command overhead). |
| Deliberate stop/start | Stop completed **0.297 s**; unit remained inactive and port/API unavailable for a checked five seconds. Start to HTTP readiness **0.688 s**. No duplicate server. |
| Unreachable Ollama | Closed loopback endpoint selected through an owned runtime-only drop-in; 65 s sampled. AI unavailable, three request errors, zero generated explanations. Both **BRAKE and EMERGENCY_BRAKE** observed from real Webots sensor frames. |
| Hung inference | Unprivileged threaded HTTP stub exposed model tags but never answered generate requests; 65 s sampled with 2 s AI timeout. Three actual timeout events measured **2.002 s each**, including retries. Four requests, one urgent cancellation. Both **BRAKE and EMERGENCY_BRAKE** observed. Stub request-arrival timestamps and timeout timestamps retained. |
| Stream pause/resume | Paused Webots only. Vivaldi status and AI badge became **STALE**, API explanation `is_current=false`, reason `no_live_data`; screenshot shows age about 11 s. Resume returned to LIVE/CURRENT, same run `8f5013ae9c3b`, observed sequence **332→431**. |
| New simulation run | Reloads produced new run IDs and sequences reset to 1 while the Pi accepted new frames. Sequences were checked within each run, not across resets. |
| System Ollama stopped | **Not performed**: system service shutdown requires sudo; no credentials requested or permissions changed. Unprivileged unreachable/hung simulations covered application failure paths. |
| Pi reboot | **Not performed or approved**. Unit enablement plus lingering is configuration evidence, not proof of reboot recovery. |

## Processing and resources

Each scenario was polled every 0.5 seconds; baseline 55 s (110 polls), unreachable
65 s (130 polls), hung 65 s (130 polls). Whole-window resource figures below include
warm-up/reload periods. Processing distributions are the last observed counters for
the fresh hazard run within each scenario. These are Pi processing times, not
sensor-to-screen end-to-end latency.

| Measurement | Real Ollama baseline | Unreachable | Hung inference |
| --- | ---: | ---: | ---: |
| Pipeline mean / p95 / max, ms | 0.973 / 2.262 / 3.818 | 0.943 / 1.280 / 1.370 | 0.971 / 1.299 / 1.424 |
| Handler mean / p95 / max, ms | 2.070 / 4.242 / 10.308 | 2.125 / 2.785 / 17.099 | 2.125 / 3.029 / 16.136 |
| Host CPU mean / max, % | 11.043 / 79.2 | 1.120 / 2.0 | 1.123 / 2.0 |
| Server CPU mean / max, % of one core | 3.058 / 11.0 | 3.288 / 5.9 | 3.655 / 5.0 |
| Server RSS max, MB | 50.1 | 50.4 | 50.5 |
| Host RAM used min–max, % | 14.0–31.9 | 31.1–31.9 | 14.4–15.1 |
| Temperature min–max, C | 44.6–54.0 | 44.6–48.5 | 45.8–49.0 |

Steady streaming was approximately 4.8 Hz; whole fault-window rates averaged 4.537
and 4.554 Hz including reload gaps. The fresh hazard runs had respectively 177,
214 and 226 measured processing samples. Measured missed/rejected frame counters
were zero in those runs; no WebSocket drops were recorded. Baseline generated two
accepted real Qwen3 explanations; completed inference mean **3884.2 ms**, maximum
**4885.3 ms**. Unavailable and hung scenarios generated no LLM output; safety
remained deterministic.

Some API polling rows contained the implemented `live-state` 404 while a restarted
server awaited its first frame: 7 after-crash rows, 2 unreachable rows, 1 hung row.
These are retained, not silently discarded; the summary's valid counts identify
rows used for processing/resource analysis. The baseline began with an old paused
snapshot before the fresh simulation started; that old run is separately retained.

Deliberate crashes/restarts and Webots reloads interrupt telemetry. Server counters
reset on restart, so zero counters in a new run do **not** establish lossless delivery
across an outage. Webots retains only the newest pending frame; frames can be skipped
while the server is unavailable. In-memory tracking and prior hazards are lost on
server restart; passed hazards may not be rediscovered. No persistence was added.

## Dashboard, APIs and logs

Vivaldi used a new temporary profile, mock Keychain and basic password-store flags,
with no personal-profile access or Chrome fallback. Dashboard LIVE, WebGL rendering
and four viewports (1440, 1024, 820, 430 px) were verified. Fault/stale/resumed/final
screenshots and displayed values are saved. The STALE screenshot was visually
inspected: telemetry is dimmed and a no-sensor-data notice appears.

Final API assertions passed for `/api/system`, `/api/live-state`, `/api/hazards`
and `/api/ai-explanation`: edge mode, real Ollama available, AI enabled, normal
20-second timeout. Hazard collection is valid but may be empty after restart,
as expected from its in-memory contract. Unified root dashboard remains active.

Logs work with `journalctl _SYSTEMD_USER_UNIT=roadsense-edge.service`; on this Pi
`journalctl --user` returned no entries. Runbook commands were corrected to the
verified system-journal filter. No journald configuration or permissions changed.
Cross-reboot journal persistence remains unverified.

## Rollback and remaining work

The preserved source backup still has SHA256
`1f64ea4190907ffef2c8c8effc81edc76fd167093edcd507d6c2f330aebe4903`.
Service rollback is `systemctl --user disable --now roadsense-edge`, verify port
free, then use the recorded manual startup command in `docs/operations.md`.
Do not unpack source archives for a service-only rollback.

Remaining: separate reboot approval and authorized reboot mechanism; actual system
Ollama shutdown if administrative access is later authorized; longer-duration soak
and cross-reboot journal checks. Restart=on-failure does not detect a hung entire
server process, and no watchdog was added. Application code/tests were unchanged;
the full unit test suite was not rerun for this operational-only change.

JSON artifacts preserve raw measurements, with known network/home/host identifiers
sanitized. Screenshots show simulated telemetry and the dashboard hostname; no
credentials or personal browser content. SHA256SUMS covers this evidence set.
