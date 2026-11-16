# RoadSense 2.0 release preflight — 17 November 2026

The existing Claude/Codex migration was preserved and staged on
`roadsense-v2-edge-ai`. No reset, cleanup or safety-algorithm change was made.
Risk and prediction match their previous controller files byte-for-byte; safety
only changes `import risk` to a relative package import.

Fresh validation:

- Active suite: 398 passed in 11.22 s, with loopback socket permission.
- Archived Arduino suite: 60 passed in 5.73 s.
- JavaScript suite: 65 passed, no failures or skips.
- Staged whitespace checks passed. Candidate filenames and source were checked
  for credential material; none was found. Ignore rules now cover local secrets,
  cloud/agent configuration, database sidecars, archives, logs and temporary files.
- Vivaldi starts in a temporary profile but its debugger times out on
  `Runtime.enable`; rendered dashboard checks did not run. No Chrome fallback.

Pi preflight at the supplied `192.168.0.104`:

| Endpoint | Observed HTTP status |
| --- | --- |
| `/api/ai-explanation` | 404 |
| `/api/system` | 404 |
| `/api/hazards` | 404 |
| `/api/live-state` | 200 |

The returned live state is `roadsense.live.v1`, run `654337898fec`, sequence 405,
simulation time 81.0, with `processing=edge`. This stored snapshot alone does not
prove a current stream or increasing sequence.

SSH to `omgaupale@192.168.0.104` failed with `Permission denied
(publickey,password)`, including outside the sandbox. Machine identity, deployed
source, service ownership and dependencies could not be inspected. No remote
backup, rsync, restart, Ollama interruption or deployment was performed.

After SSH access is restored, follow deployment.md: verify identity and service,
create and validate a named backup, review the checksum rsync dry run, then obtain
approval before replacing the running installation. Live Webots acceptance,
Qwen3 inference and host/stream measurements remain outstanding.
