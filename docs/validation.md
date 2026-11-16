# RoadSense 2.0 handoff validation

Local checks performed on 16–17 November 2026 (Asia/Kolkata). This report distinguishes the
existing migration, changes made during continuation, and checks requiring the Raspberry Pi.
No commit, push, reset, pull, untracked-file cleanup or remote deployment was performed.

## Recovered state

The checkout was on `main`, with a substantial uncommitted migration. Before edits it already had:

- The standard-library `roadsense.autonomy` package shared by Webots and the edge server.
  In edge mode the controller sends raw sensors and does not run its local pipeline.
- One dashboard at `/`, the `/autonomy` redirect, and removal of the active Arduino UI/API.
- Strict sensor-frame ingestion, server-side pipeline execution and live WebSocket publication.
- The asynchronous AI explainer, bounded stage metrics, host diagnostics, and hazard export.
- Arduino firmware, protocol, drivers, desktop app and storage tests under `legacy/arduino`.
  The archived firmware was verified byte-for-byte against Git HEAD's original firmware.
- Documentation for most of those features, but a largely obsolete Arduino-focused README.

The original working tree and `.git` were archived before source edits. The archive contains
35,189 entries, including untracked migration files and existing development environments:

`/Users/smash_mario/Desktop/PROJECTS/ROADSENSE-backup-20261116-codex-handoff.tar.gz`

Its contents were inspected and its copied bytes checked against the original temporary archive.
SHA-256: `503670047bbf61d9a0a7be91ab1eb027dd23c42ec52dd00728cb454364c71c37`.
To recover, extract into a separate empty directory and inspect that copy first. Do not reset or
extract over the current checkout. This is the handoff state, not the post-fix state.

## Changes made during continuation

| Files | Change |
| --- | --- |
| `src/roadsense/web/ai_explainer.py` | End markers cancel inference and clear pending work; late answers cannot enter the cache after a run ends. No-decision states retain correct run identity and do not bypass old-request cancellation. Explanation source metadata now includes qualitative cause facts. |
| `src/roadsense/web/live.py` | Removes the producer's processing claim on precomputed ingestion. Only actual sensor processing can label a state as computed on this edge server. |
| `src/roadsense/web/static/js/system.js`, `view.js` | Explanation freshness also compares threat type and qualitative facts, including hazard severity and lane or vehicle position. A changed cause is marked UPDATING even if action and threat id match. |
| `tests/test_ai_explainer.py`, `tests/test_web_live.py`, `tests/js/system.test.mjs` | Regression coverage for lifecycle, provenance and explanation freshness; source-schema expectations updated. |
| `tests/browser_smoke.py` (new) | Repeatable local browser assertions using installed Chromium, a temporary profile and the existing web dependencies. No browser dependency or frontend build step was added. |
| `README.md` | Replaced retired-product instructions with the current architecture, direct Python launch, simulation setup, limitations and links. |
| `docs/deployment.md`, `deploy/roadsense.service` | Source-first Python path, verified-address preflight, checksum dry run, exact-process stop, backup verification and explicit-backup rollback. Corrected OOM-priority claims. |
| `docs/live-state.md`, `simulation/webots/README.md` | Direct Python commands, canonical dashboard route, Pi port 8001 and edge/precomputed distinction. |
| `docs/dashboard.md`, `docs/ai-explanations.md`, `docs/edge-metrics.md`, `docs/hazard-records.md`, `docs/architecture.md` | Corrected freshness, skipped-event, timing-boundary, cancellation, resource and deduplication claims. |
| `docs/validation.md` (new) | This evidence and remaining-work record. |

No files were deleted or newly archived during continuation. The many deletions and archive moves
in `git status` were already present at handoff and have been preserved. No new runtime dependency
was introduced, and deterministic safety algorithms were not changed.

## Automated results

| Check | Result |
| --- | --- |
| Original active suite, before fixes | **391 passed in 11.10 s**, matching the handoff count |
| Final active suite | **395 passed in 11.22 s** |
| JavaScript, run independently | **65 passed**, 0 failed, 0 skipped (64 before fixes) |
| Archived Arduino tests | **60 passed in 5.71 s** |
| Browser regression | Passed, including four viewport sizes and lifecycle/failure scenarios below |
| `git diff --check` | Passed |
| Local Markdown file links | Checked after documentation updates |

The active Python suite includes a wrapper around the Node suite: the 65 JavaScript checks are
not 65 additional pytest test cases. The final Python count adds four cases to the original 391.

Commands used:

```sh
venv/bin/python -m pytest -q
venv/bin/python -m pytest legacy/arduino/tests -q
node --test tests/js/
```

The existing test interpreter is Python 3.12.14 and was used directly, without activation.
The user-owned runtime directory was used successfully with `python3.12` for the browser's
real server. It lacked pytest, so that initial direct pytest attempt could not start.
The first sandboxed regression attempt produced 373 passes, 12 failures and 6 setup errors;
all failures/errors were blocked loopback socket binds. With local socket permission the
unchanged baseline passed all 391 tests. No test was removed or skipped to hide a failure.

Coverage includes strict sensor validation, edge computation, tracking/prediction/risk/safety,
publication, real local HTTP/WebSocket exchanges, AI unavailable/hung/cancelled/late answers,
SIGINT/SIGTERM shutdown, metrics, hazard records, retired routes and local module imports.
AI successful responses in these tests come from fakes, not a real Ollama model.

## Browser evidence

### Vivaldi preference update (17 November 2026)

All subsequent RoadSense browser work uses `/Applications/Vivaldi.app`; the smoke
test now defaults to `/Applications/Vivaldi.app/Contents/MacOS/Vivaldi` and supports
`--headed`. It retains temporary profiles and Keychain-avoidance flags, disables
extensions, and attempts all cleanup even when debugger shutdown fails. No Chrome
download, launch or automatic browser fallback is allowed; see [AGENTS.md](../AGENTS.md).

Vivaldi launched and exposed its DevTools endpoint, but `Runtime.enable` timed out
before dashboard assertions in both headless and visible-window runs. Explicitly
selecting the `about:blank` test target did not resolve it. This runner/Vivaldi
compatibility issue remains unresolved; these attempts are **not** a passing
Vivaldi visual or responsive regression. Temporary profiles were removed and local
test servers shut down after the failed runs. A process check found no remaining
test browser/server processes, and the temporary-profile count was zero. The
active test suite passed **398 tests in 11.12 s** with local socket permission,
including three new browser-configuration/failure-cleanup checks; `git diff
--check` passed. Application source and existing regression assertions were preserved.

Current command (uses installed Vivaldi; never installs a browser):

```sh
PYTHONPATH=src venv/bin/python tests/browser_smoke.py
```

### Historical browser run before the Vivaldi preference

The installed `chrome-headless-shell` was launched with an isolated temporary profile,
`--password-store=basic` and `--use-mock-keychain`. No personal browser profile, credentials
or Keychain changes were used. The server ran on the Mac, with scripted sensor frames.

```sh
PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python312" \
python3.12 tests/browser_smoke.py --browser /path/to/chrome-headless-shell
```

The browser path used on this Mac was:

`/Users/smash_mario/Library/Caches/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell`

Results and screenshots: `/private/tmp/roadsense-browser/` (temporary artifacts).

- 1440×900, 1024×768, 820×1180 and 430×932: no horizontal page overflow; screenshots visually inspected.
- WebGL2 initialized with software rendering. The ego car, tracked vehicle, predicted path,
  hazard labels, BRAKE recommendation and measured edge timings rendered.
- NO PRODUCER → LIVE → STALE → SIMULATION ENDED, plus DISCONNECTED and reconnection after
  server restart. The explanation moved from CURRENT to STALE with the stream.
- Ollama connection refused: availability displayed correctly; deterministic explanation and
  safety processing continued. This scenario's HIGH-risk BRAKE also uses deterministic text by policy.
- WebGL disabled: the explicit fallback appeared while safety panels remained operational.
- An HTML-like safety reason rendered literally; no element or injected code executed.
- No legacy navigation. No normal-operation browser JavaScript errors. The deliberately disabled
  WebGL case produced two expected console errors. Chromium also logged host display/software-GPU
  diagnostics; these are not application exceptions.

The smoke test's first attempts failed on test assertions that did not match the rendered labels;
the assertions were corrected after inspecting `main.js`. The application was not changed to satisfy
those label assumptions. This is not a long-duration Webots run, physical GPU benchmark or Pi test.
Closely aligned 3D labels can overlap; no new label-placement system was added.

## Deployment status and remaining work

The Mac can run and demonstrate the unified application with synthetic sensor frames. The final
source and frontend changes were reviewed against the pre-edit backup. Documentation includes a
concrete deployment/rollback procedure, but Pi-specific preflight is incomplete:

- Read-only SSH to historical `192.168.0.104` failed with “Address already in use” outside the sandbox.
- Read-only SSH to `SmashMario.local` failed because the hostname could not be resolved.
- Pi process/port, deployed source diff, remote backup, actual host metrics and real Ollama state
  could not be verified. Nothing was written to the Pi.
- The final code has not been deployed or tested with live Mac Webots → Pi → browser traffic.
  Historical Pi throughput, temperature and model-latency observations remain historical.

The next milestone is **one authorized Pi end-to-end acceptance run**: recover its reachable
address, inspect and back up its current installation, review the checksum dry run, deploy this
verified source, then measure simultaneous sensor processing and real Qwen3 explanations.
Record run/sequence freshness, processing percentiles, stream gaps, model errors, memory,
temperature and throttling. Deployment, remote backup and process/service changes still require
explicit authorization.

Durable hazard storage, real GNSS, geographic mapping/sync and a trained decision model remain
future milestones. Recommendations remain advisory; heuristic model-output filtering does not
prove semantic correctness or real-vehicle safety.
