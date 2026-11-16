# RoadSense operational reliability

## Verified baseline and proposal — 17 November 2026

The live server is manually launched PID 2849, owned by omgaupale, with parent PID 1.
Its working directory is `/home/omgaupale/roadsense-edge`; port 8001 has one listener.
Python is 3.13.5, systemd is 257.13, and no system or user RoadSense unit is installed.
Ollama is an enabled system service running as `ollama`, on localhost:11434.
The `omgaupale` user manager is available and `loginctl show-user omgaupale -p Linger`
returns `Linger=yes`. No lingering or sudo permission change is needed.

Working manual startup (recorded selected environment, no credentials):

```sh
cd ~/roadsense-edge
nohup env PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python313" \
  ROADSENSE_AI_EXPLAIN=1 \
  python3 -m roadsense.web --host 0.0.0.0 --port 8001 --no-browser \
  </dev/null >/tmp/roadsense-v2-deployment.log 2>&1 &
```

The approved [user service](../deploy/roadsense-edge.user.service) is installed as
`~/.config/systemd/user/roadsense-edge.service` and enabled under `default.target`.
The existing `deploy/roadsense.service` system-service example remains an alternative;
do not enable both. The user service runs the same checkout and dependency directory,
with unbuffered logs and bytecode writes disabled. Its explicit AI URL/model match the
working defaults. It restarts after crashes in 3 seconds, limits repeated failed starts
to five per minute, and allows ten seconds for SIGTERM shutdown before forced cleanup.
It has no dependency on the system Ollama unit. Ollama availability must never determine
whether the safety server runs. Application health is checked through HTTP after startup;
`Type=exec` alone does not assert HTTP readiness. There is no watchdog for a hung entire
Python process in this proposal; bounded AI-request timeouts already live in the worker.

This configuration was installed and verified after explicit approval. See the
[Phase 5 measured results](evidence/phase5/RESULTS.md). Boot enablement is verified;
one controlled reboot recovery is now verified in
[reboot results](evidence/phase5/REBOOT_RESULTS.md). The baseline
above describes the pre-cutover state.

## Cutover after explicit approval

1. Recheck PID, command, working directory, port ownership, unit absence and Linger=yes.
   Preserve the selected current environment and any unit/drop-ins if state has changed.
2. Verify the named source backup remains intact:
   `/home/omgaupale/roadsense-edge-backup-20261117-014533.tar.gz`, SHA256
   `1f64ea4190907ffef2c8c8effc81edc76fd167093edcd507d6c2f330aebe4903`.
   No source deployment is needed for this phase.
3. Copy only the approved unit to its user-unit path. Validate with
   `systemd-analyze --user verify ~/.config/systemd/user/roadsense-edge.service`.
4. Gracefully terminate only the reverified manual PID; wait for process exit and
   port 8001 to become free. Do not start systemd if the port remains occupied.
5. `systemctl --user daemon-reload` then `systemctl --user enable --now roadsense-edge`.
6. Require one listener and one server, MainPID matching the listener, healthy APIs,
   AI enabled, correct checkout import, and actual advancing Webots sensor frames.
7. If cutover fails, stop/disable the new unit, verify the port is free, and restart
   the recorded manual command above. Preserve logs. Do not unpack a source backup
   unless source corruption is demonstrated; this cutover changes no application files.

## Recovery acceptance plan (not yet executed)

Save timestamped JSON evidence under `docs/evidence/phase5/`, with sanitized identifiers.
Measure host CPU, process CPU/RSS, total RAM, temperature/throttling, processing/handler
mean/p95/max, frames accepted/rejected/missed, sequence gaps, WebSocket drops, stale age,
AI latency/errors, recovery time and HTTP outages. Use fixed polling intervals and retain
raw samples. Compare sequences only within the same run identity.

For each applicable AI fault replay the existing Webots/SUMO hazard scene on the Mac:
observe BRAKE and EMERGENCY_BRAKE from Pi processing, never from model output. Record
at least 45 seconds per scenario, and allow bounded recovery sampling afterward.

| Scenario | Injection and expected evidence |
| --- | --- |
| Baseline | Real qwen3:0.6b; actual accepted LLM explanation, live hazards, source run/sequence and four APIs. |
| System Ollama stopped | With separate approval and administrative access, `sudo systemctl stop ollama`; keep RoadSense PID unchanged, confirm unavailable/fallback and both brake actions; restore with `sudo systemctl start ollama` in cleanup. Existing sudo is password-protected; do not collect passwords or modify sudo policy. |
| Ollama unreachable | Temporary **runtime-only** user-unit drop-in changes AI URL to a verified closed loopback port. Restart RoadSense, replay threats, verify fallback and advancing frames. Remove only that owned drop-in and restore service afterward. |
| Hung inference | Temporary standard-library HTTP stub on a free loopback port answers `/api/tags` with the configured model, accepts `/api/generate` without replying. Runtime-only drop-in selects stub URL and a 2-second AI timeout. Require an actual nonurgent request, observed timeout/retry, both brake actions and bounded frame processing. Stop only the owned stub and remove only owned test drop-in in finally cleanup. Restore the normal 20-second timeout. |
| Stream interrupted | Pause Webots (not the Pi), wait beyond stale threshold, verify STALE and explanation freshness in Vivaldi; resume same run and check sequence progression without synthetic end marker. Reload Webots separately to verify new run identity and reset sequence handling. |
| Application crash | After cutover approval, send SIGKILL only to the unit MainPID. Measure NRestarts, changed PID and elapsed time until APIs and Webots stream recover. Verify one port listener. In-memory state is expected to reset. |
| Graceful stop/start | Stop and start via user systemd, verify no duplicate server and bounded shutdown. A deliberate stop must remain stopped. |
| Reboot | **Separate explicit reboot approval**, plus a way for the user to reboot if sudo is unavailable. Record boot ID beforehand, then verify changed boot ID, enabled unit, startup without interactive login, single listener, all APIs, and reconnection to Mac Webots. Configuration is not proof of reboot recovery. |

Faults must be injected one at a time, with recorded original settings and finally cleanup.
Keep Webots on the Mac and pipeline processing on the Pi. Never edit safety algorithms for
these tests. Vivaldi uses a temporary profile and Keychain-avoidance flags per AGENTS.md;
close owned browser processes and remove their profiles after validation.

## Operator commands after approved installation

Run on the Pi as omgaupale, without sudo:

```sh
systemctl --user start roadsense-edge
systemctl --user stop roadsense-edge
systemctl --user restart roadsense-edge
systemctl --user status roadsense-edge --no-pager
systemctl --user is-enabled roadsense-edge
systemctl --user show roadsense-edge -p MainPID -p NRestarts -p Result
journalctl _SYSTEMD_USER_UNIT=roadsense-edge.service -n 100 --no-pager
journalctl _SYSTEMD_USER_UNIT=roadsense-edge.service -f
ss -ltnp | grep :8001
curl --fail http://127.0.0.1:8001/api/system
```

On `start-limit-hit`, inspect journal and port ownership first, correct the cause, then
`systemctl --user reset-failed roadsense-edge` and start it again. Never repeatedly launch
manual servers while the unit is enabled. `GET /api/live-state` returns 404 before any frame;
this is not an application-startup failure. A healthy system API with stale telemetry means
inspect Mac Webots and its sensor publisher. Check Ollama independently with
`systemctl status ollama` and `ollama list`; its failure should affect explanations only.

Rollback to manual management: `systemctl --user disable --now roadsense-edge`; verify
the unit is inactive and port free; run the recorded manual command. Keep the disabled unit
for diagnosis. Do not disable lingering, which predates RoadSense and may serve other apps.
Never restore source archives over a running process or delete unrelated configuration.

## Verified Phase 5 operating state

The exact approved user unit is installed and enabled; final MainPID was 4899.
Restart=on-failure, RestartSec=3, TimeoutStopSec=10 and Linger=yes were verified.
Normal AI URL is localhost:11434, model qwen3:0.6b, timeout 20 seconds. No runtime
drop-ins remain. The hung test service is inactive and its owned temporary files
were removed. No application source/dependency changes, sudo, reboot, commit or
push occurred. The named source backup remains intact.

On this Pi `journalctl --user` returned no entries, while the system-journal
filter `_SYSTEMD_USER_UNIT=roadsense-edge.service` returned real application
logs as the ordinary user. Use the verified commands above; no journal permissions
were changed. Journal persistence across reboot has not been verified.

A process restart resets in-memory tracking, metrics, live state and hazard records.
Passed hazards are not automatically reconstructed; do not mistake a new empty
hazard collection for durable storage. Restart recovery includes a temporary
period with no live state until Webots publishes again. See measured outages in
the results; counter resets prevent claiming zero total frame loss across crashes.

## Controlled reboot preflight and completed test

The separately approved reboot preflight reconfirmed service active/enabled,
MainPID 4899, Linger=yes, the exact installed unit checksum, existing SSH access
and the preserved backup checksum. No active rsync/scp/Git/package operation was
found. Existing desktop and shell sessions will disconnect during reboot. The
user confirmed physical keyboard/display recovery access.

No reboot was issued by the agent: noninteractive sudo requires a password and
logind CanReboot returns `challenge`. The user was instructed to run exactly one
`sudo systemctl reboot` at the Pi console, with credentials entered only there.
A bounded passive monitor was started before that action. It records SSH-port
and HTTP recovery from the first observed outage; this reference is not the exact
console-command timestamp. It checks new HTTP startup before authenticating SSH
so the SSH login cannot explain RoadSense startup. The user subsequently issued the reboot locally. All post-boot checks passed;
see [measured reboot results](evidence/phase5/REBOOT_RESULTS.md).

If SSH does not return, do not reboot again: at the local console check
`hostname -I`, `systemctl status ssh`, `systemctl --user status roadsense-edge`,
`loginctl show-user omgaupale -p Linger`, and
`journalctl -b _SYSTEMD_USER_UNIT=roadsense-edge.service --no-pager`. Record the
errors before modifying service configuration.

## Verified post-reboot state

The single separately approved local-console reboot completed. MainPID is now
1037, active/enabled, with no test drop-ins and no manual application start.
RoadSense HTTP was observed before the monitor authenticated SSH. Existing Webots
reconnected automatically; a subsequent scene replay verified real Qwen3 outputs,
both brake decisions, hazards and Vivaldi LIVE rendering. The exact approved unit
and backup checksums are unchanged. No additional reboot is authorized.

Observed outage-to-HTTP was 110.636 seconds, to authenticated SSH 110.957 seconds,
and to first fresh edge telemetry 117.198 seconds. These are relative to the first
observed network outage, not the unrecorded local-console command time. Systemd
reported 9.200 seconds kernel/userspace startup; the difference is not attributed
without shutdown evidence. Only the current boot journal is visible; prior-boot
logs were unavailable. Investigate the long disruption before claiming fast reboot
recovery; any logging/service configuration changes require separate approval.

## Phase 5 closed; outage diagnosis remains open

See [closure analysis](phase5-closure.md) for the measured boot/network/service
timeline, unavailable shutdown interval, and correction that 559 skipped snapshots
is a cumulative publisher counter, not reboot-only loss. The current healthy unit
was not changed or restarted for closure. Vendor journald configuration explicitly
uses Storage=volatile; persistent logging would require separate approval and
bounded retention. Future gap records are preferred over replaying stale frames
into the live safety path. Exact commit scope is in
[commit proposal](phase5-commit-plan.md).
