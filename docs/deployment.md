# Running and deploying RoadSense

RoadSense runs with plain Python, without mandatory virtual-environment activation, Docker or
a frontend build. Keep the checkout's `src/` first on `PYTHONPATH` so an older installed package
cannot hide local changes. Use one server process; state is held in memory.

| Where | Source | Runtime |
| --- | --- | --- |
| Mac | `~/Desktop/PROJECTS/ROADSENSE` | Python 3.12; default local port 8000 |
| Raspberry Pi 5 | `/home/omgaupale/roadsense-edge` | Python 3.13; edge port 8001 |

`192.168.0.104` is a historical Pi address, not a guaranteed current address. Confirm it through
`SmashMario.local`, the router's DHCP list, or `hostname -I` on the Pi before deployment.
Changes on the Mac do not reach the Pi automatically. **Remote backup, synchronization,
service installation and restart require explicit deployment authorization.**

## On the Mac

From the repository root, use the existing runtime dependencies:

```sh
PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python312" \
python3.12 -m roadsense.web --no-browser
```

Open `http://127.0.0.1:8000/`. The user-owned dependency directory can be populated on a fresh
machine with `python3.12 -m pip install --target "$HOME/.local/share/roadsense/python312" '.[web,dev]'`.
Do not install into Homebrew's managed environment or use `--break-system-packages`.

For tests, this Mac already has a Python 3.12 interpreter with test dependencies:

```sh
venv/bin/python -m pytest -q
venv/bin/python -m pytest legacy/arduino/tests -q
node --test tests/js/
```

This uses an existing interpreter directly; no environment activation is required. Alternatively,
after installing `dev` into the user-owned dependency directory:

```sh
PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python312" python3.12 -m pytest -q
```

The archived suite also needs pyserial. Node 22.7+ runs the ES-module tests without a build step.
Tests that open loopback HTTP/WebSocket sockets need local networking permission.

For Webots, use the launch environment in the [README](../README.md#run-on-the-mac), with
`ROADSENSE_EDGE_URL=http://PI_IP:8001/api/sensor-frame` for the Pi, or
`http://127.0.0.1:8000/api/sensor-frame` for local development. The dashboard is always `/`.

## Read-only deployment preflight

Set the verified address on the Mac:

```sh
ROADSENSE_PI=omgaupale@SmashMario.local
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 "$ROADSENSE_PI" '
  hostname; hostname -I; python3 --version
  ls ~/roadsense-edge/src/roadsense/web
  pgrep -af "[r]oadsense.web"
  systemctl list-units --all --type=service --no-pager | grep -i roadsense
  ss -ltnp | grep -E ":8001|:11434"
  ollama list
'
```

Confirm the machine identity, source layout, process owner and exact PID, and whether a systemd
unit owns the process. Ollama should listen on `127.0.0.1:11434`. If SSH or identity verification
fails, stop here; do not guess a replacement host or change SSH credentials.

Review a checksum-based dry run against the current Pi source:

```sh
rsync -anic --delete --exclude '__pycache__/' \
  src/roadsense/ "$ROADSENSE_PI":roadsense-edge/src/roadsense/
```

`-n` prevents writes and deletions. Expect the unified dashboard and shared `autonomy/` package,
with the retired Arduino modules and old dashboard assets removed from the deployed package.
Inspect unexpected differences before proceeding. Only the runtime package is synchronized;
`legacy/`, simulation, tests and development environments stay on the Mac.

## Deployment after authorization

First run the local regression suite and review the dry run above. Then back up the entire Pi
source directory, including hidden files and any Git metadata. This command **writes a backup
on the Pi**, so it belongs after authorization:

```sh
ssh "$ROADSENSE_PI" '
  set -eu
  cd "$HOME"
  backup="roadsense-edge-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  tar -czf "$backup" roadsense-edge
  tar -tzf "$backup" >/dev/null
  printf "Verified backup: %s/%s\n" "$HOME" "$backup"
'
```

Record that exact backup filename and how the old server was launched. Do not select a backup
by “newest” during rollback. Source archives do not back up external dependency directories,
systemd configuration or Ollama models; preserve any existing service file before replacing it.

Stop the **identified** service with `sudo systemctl stop <actual-unit>`. If it is manually
started, use Ctrl+C in its terminal or `kill -TERM <verified-PID>`. Verify port 8001 is no longer
listening before copying. Do not use broad `pkill -f` commands: they can match unrelated
processes or the remote shell, and an active service may immediately restart the old server.

Repeat the reviewed dry run after stopping, then copy:

```sh
rsync -avc --delete --exclude '__pycache__/' \
  src/roadsense/ "$ROADSENSE_PI":roadsense-edge/src/roadsense/
```

Run the same dry run again; it should report no remaining content differences. Start manually
on the Pi first:

```sh
cd ~/roadsense-edge
PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python313" \
ROADSENSE_AI_EXPLAIN=1 \
python3 -m roadsense.web --host 0.0.0.0 --port 8001 --no-browser
```

Omit the AI flag for deterministic explanations only. Confirm the existing dependencies import
successfully with this interpreter before changing them. If missing, install `web` dependencies
into the user-owned Python 3.13 directory; no system-wide pip changes are needed.

## Verify the deployed version

From the Mac, use the verified Pi address:

```sh
curl --fail --max-time 5 http://PI_IP:8001/api/system
curl --fail --max-time 5 http://PI_IP:8001/api/ai-explanation
curl --fail --max-time 5 http://PI_IP:8001/api/hazards
```

Confirm `app.version` is `2.0.0.dev0` and host identity matches the Pi. Start Webots with the
sensor endpoint and open `http://PI_IP:8001/`. Confirm:

- Live state has `simulation.processing = "edge"`, with increasing run sequences.
- `/api/system` has `edge.mode = "edge"` and real `process` samples, not only HTTP GET timings.
- The dashboard becomes LIVE, shows hazards and recommendations, and `/autonomy` redirects to `/`.
- AI source run/sequence matches the displayed decision. Pausing input makes it stale.
- With the AI flag on, Ollama availability, actual accepted answers, errors and inference duration
  are measured. Deterministic fallback alone does not prove the real model works.
- An unavailable or slow Ollama does not stop sensor processing. Measure gaps and processing
  percentiles during inference, alongside memory, temperature and throttling.

Record duration, accepted/missed/rejected frames, stage percentiles and inference errors as new
results. The historical 4.63 Hz stream and approximately 4.07 s inference average are not
acceptance results for this version.

## Optional systemd service

For this Pi, the approved user service is installed and boot recovery is verified.
Use the [operations runbook](operations.md) for management and rollback and the
[Phase 5 closure](phase5-closure.md) for measured results and remaining diagnostics.
It uses existing Linger=yes and direct-Python dependencies. The system-service
example below is an alternative; do not enable both user and system RoadSense units.

After manual validation, stop that manual process. Review `deploy/roadsense.service` for this
Pi's paths and preserve any existing unit and its enabled state before installation:

```sh
scp deploy/roadsense.service "$ROADSENSE_PI":roadsense-edge/roadsense.service
```

On the Pi, after the existing unit has been backed up if present:

```sh
sudo install -m 0644 ~/roadsense-edge/roadsense.service /etc/systemd/system/roadsense-edge.service
sudo systemctl daemon-reload
sudo systemctl enable --now roadsense-edge
systemctl status roadsense-edge --no-pager
journalctl -u roadsense-edge -f
```

The unit enables AI explanations, runs as `omgaupale`, uses port 8001 and has no virtual-environment
dependency. `OOMScoreAdjust=-500` makes the server less likely to be killed under memory pressure;
it does not guarantee which process the kernel will kill or uninterrupted processing.

## Persistent history (deployed 17 November 2026)

The history store is off unless `ROADSENSE_HISTORY_ENABLE=1`. On the Pi it is on, events only,
through the user drop-in `~/.config/systemd/user/roadsense-edge.service.d/history.conf`; the unit
file is unchanged. Rollout, acceptance results and rollback:
[history.md](history.md#raspberry-pi-deployment-phase-6b). A future source deployment must keep
the drop-in (it lives outside `~/roadsense-edge`) and copy `history.py` and `history_export.py`
with `server.py`.

## Rollback

Stop the exact process/service that was started for the deployment. On the Pi, choose the
**recorded, verified backup filename** explicitly:

```sh
cd "$HOME"
ROADSENSE_BACKUP="$HOME/roadsense-edge-backup-YYYYMMDD-HHMMSS.tar.gz"
tar -tzf "$ROADSENSE_BACKUP" >/dev/null && \
  mv roadsense-edge "roadsense-edge-failed-$(date +%Y%m%d-%H%M%S)" && \
  tar -xzf "$ROADSENSE_BACKUP" -C "$HOME"
```

The failed deployment is preserved. Restore the old unit file and enabled state if those were
changed, reload systemd, and start the previous version by its recorded launch method. Verify
port 8001 and its APIs again. Do not blindly disable an existing service or overwrite a previous
rollback directory.

## Limits

There is no login or encryption; use a trusted LAN. Ollama stays on localhost. The app has no
durable hazard database, and restarting loses run-local state. Qwen3's historical loaded size
was about 1 GB; the 4 GB Pi still needs a live resource test with this version. Reducing inference
threads limits concurrency but does not reserve a physical CPU core for the pipeline.
