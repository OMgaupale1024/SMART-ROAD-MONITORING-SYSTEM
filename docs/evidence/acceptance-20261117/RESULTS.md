# RoadSense 2.0 Pi acceptance — 17 November 2026

Deployed existing commit 64256d46d11a830d3fe160c2afe6b5f9a8623d79 from roadsense-v2-edge-ai.
Only the approved 17 runtime files were synchronized, with an explicit files-from manifest and no deletion option. Final checksum dry run was empty. No commits, pushes or repository changes were made during deployment.

Backup preserved: /home/PI_USER/roadsense-edge-backup-20261117-014533.tar.gz
SHA256: 1f64ea4190907ffef2c8c8effc81edc76fd167093edcd507d6c2f330aebe4903

The old manually launched PID 1701 was gracefully stopped. Final server PID 2849 is the sole port-8001 server. No systemd unit was installed. Runtime: Python 3.13.5, checkout src first, existing ~/.local/share/roadsense/python313 dependencies, ROADSENSE_AI_EXPLAIN=1. No environment activation or new dependency installation.

Final GET /, /api/live-state, /api/system, /api/hazards and /api/ai-explanation returned 200. /autonomy resolved to /. Before producer startup, live-state returned its expected 404. Final system version: 2.0.0.dev0. Host: Raspberry Pi 5, PI_HOSTNAME.

Existing Webots/SUMO simulation was reloaded through its UI and published actual raw sensor frames to the Pi. processing=edge, sequences increased within each run. Baseline decisions included BRAKE, SLOW_DOWN, MONITOR and MAINTAIN. Four simulated pothole records were returned with edge provenance, world coordinates, and null geographic coordinates.

Baseline: 45 polls over 47.0 seconds, sequences 157 to 374. No missed/rejected frames or dropped WebSocket messages. Stream around 4.8 Hz. Pipeline processing mean 0.864 ms, p95 2.026 ms, max 3.274 ms; full handler mean 1.831 ms, p95 3.989 ms, max 9.874 ms. These are server stage timings, not end-to-end network latency. Dashboard recorded an earlier 2.805-second longest gap; deliberate restarts/reloads also interrupt streaming.

Real qwen3:0.6b generated accepted explanations, including the medium pothole in the right lane (2872.1 ms client latency; 2865.7 ms Ollama total; 40 eval tokens). Initial completed-request latency distribution: mean 4047.9 ms, p95/max 6252.9 ms. Later final restored-session distribution: mean 3616.8 ms, p95/max 5235.6 ms. One unsupported-output rejection was observed; late/cancelled outputs were also discarded, and deterministic fallback remained active.

Baseline host CPU mean 12.79%, max 77.8%; server CPU mean 3.19%, max 19.0%. Server RSS 49.8–50.9 MB. Host RAM used 31.3–32.5% (maximum about 1315 MB). Temperature 46.9–55.1 C. No throttling or swap use observed.

Failure isolation: sudo required a password, so system Ollama was not stopped. Instead the application temporarily used a closed loopback Ollama endpoint; real Ollama URL was restored in finally cleanup. During a fresh Webots hazard encounter, availability=unavailable, sequence 1 to 98 increased, BRAKE and EMERGENCY_BRAKE decisions continued. Hazard-run process mean 1.026 ms, p95 1.356 ms, max 1.506 ms; zero missed/rejected frames. The first test harness checked too early for a post-restart frame; a later harness assertion incorrectly compared sequences across the intentional Webots run reset. Saved samples were revalidated grouped by run identity and passed. Actual Ollama service-stop and hung-request timeout acceptance were not performed.

Vivaldi: isolated temporary profile, password-store=basic, mock keychain, extensions disabled. Direct navigation to the Pi URL allowed CDP automation; no Chrome fallback. LIVE status and WebGL2 confirmed. Viewports 1440x900, 1024x768, 820x1180 and 430x932 had no horizontal overflow; desktop/phone screenshots visually inspected. No exceptions observed during the brief CDP check (not a complete session console audit). Browser and temporary profile closed/removed after testing. Webots remains running and server uses restored real Ollama endpoint.

Prior fresh predeployment Python suite: 398 passed in 11.13 s; tests were not rerun after file-only deployment. Existing earlier archived/JS results were 60/65 passes. This is a bounded simulation acceptance run, not a long-duration or physical-vehicle validation. Remaining old Pi package files were preserved as required; only the updated root dashboard is mounted by the active server.

Evidence: baseline.json, ollama-unreachable.json, ollama-unreachable-threats.json, final-api.json, dashboard-text.json, vivaldi-*.png in this directory. Logs on Pi: /tmp/roadsense-v2-deployment.log. Temporary evidence can disappear after system cleanup.
