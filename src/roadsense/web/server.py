"""RoadSense edge server: sensor ingestion, the autonomy pipeline's live state, and the one dashboard that shows it.

Routes (docs/architecture.md):
  POST /api/sensor-frame    a Webots sensor frame; the pipeline runs here and its state is published
  POST /api/live-state      a state computed by the producer itself (standalone Webots)
  GET  /api/live-state      the latest state
  WS   /ws/live             every state, to any number of clients
  GET  /api/system          edge-processing and host measurements (read-only)
  GET  /api/ai-explanation  the explanation of the latest safety decision (read-only)
  GET  /api/hazards         the hazard map as roadsense.hazard.v1 records (read-only)
  GET  /                    the dashboard; /autonomy, its old address, redirects here

There is no login: serve it to a trusted local network only.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from roadsense import __version__
from roadsense.autonomy import prediction, risk, safety
from roadsense.web import ai_explainer, hazard_records, live, metrics

# one of each per app run, created by lifespan()
live_hub: Optional[live.LiveStateHub] = None  # the latest live state and its WebSocket clients
explainer: Optional[ai_explainer.AIExplainer] = None  # words the safety decision; never part of making it
host: Optional[metrics.Host] = None  # this computer's load
started = (0.0, 0.0)  # when this app run began: (wall clock, monotonic)

NO_CACHE = {"Cache-Control": "no-cache"}  # browsers revalidate: a deployed update is never mixed with cached files
# The safety processor's own constants, for the dashboard's reference lines: it draws them, it never applies them.
SAFETY = {"critical_ttc_s": risk.CRITICAL_TTC_S, "high_ttc_s": risk.HIGH_TTC_S, "horizon_s": prediction.HORIZONS_S[-1],
          "nominal_speed_kmh": safety.DEFAULT_POLICY.nominal_speed_kmh}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global live_hub, explainer, host, started
    config = ai_explainer.config_from_environment()
    explainer = ai_explainer.AIExplainer(config)
    live_hub = live.LiveStateHub(listeners=[explainer.observe])
    host = metrics.Host()
    started = (time.time(), time.monotonic())
    print("[RoadSense] AI explanations: %s" % (
        "model %s at %s" % (config.model, config.url) if config.enabled
        else "off (deterministic explanations; set ROADSENSE_AI_EXPLAIN=1 to use Ollama)"), flush=True)
    try:
        yield
    finally:
        explainer.close()


# The UI is served from this app, so it is same-origin: no CORS middleware. Swagger UI and ReDoc load their
# scripts from a CDN, which an offline edge device cannot reach: the API is described in docs/ instead.
app = FastAPI(title="RoadSense Edge Server", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"


# --- Live RoadSense state (docs/live-state.md)
# async handlers: the hub and its client queues belong to the event loop


@app.post("/api/live-state")
async def ingest_live_state(request: Request):
    try:
        return live_hub.ingest(await live_body(request))
    except live.LiveStateError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


@app.post("/api/sensor-frame")
async def ingest_sensor_frame(request: Request):
    received = time.perf_counter()
    try:
        body = await live_body(request)
        return live_hub.ingest_sensors(body, received, time.perf_counter() - received)
    except live.LiveStateError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


async def live_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > live.MAX_BYTES:
            live_hub.metrics.reject(413)
            raise live.LiveStateError(413, "live input over %d bytes" % live.MAX_BYTES)
        body.extend(chunk)
    return bytes(body)


@app.get("/api/live-state")
async def get_live_state():
    if live_hub.latest_json is None:
        raise HTTPException(status_code=404, detail="No live state received yet")
    return Response(content=live_hub.latest_json, media_type="application/json")


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    await websocket.accept()
    queue = live_hub.subscribe()

    async def forward() -> None:
        try:
            while True:
                await websocket.send_text(await queue.get())
        except Exception:
            pass  # client gone; the receive loop below sees the disconnect

    sender = asyncio.create_task(forward())
    try:
        # Clients send nothing; listening is how a closed tab is noticed while no state arrives.
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass
    except Exception:
        pass
    finally:
        sender.cancel()
        live_hub.unsubscribe(queue)


# --- Read-only diagnostics for the dashboard
# plain handlers: they run in a worker thread, so reading the host never delays a sensor frame on the event loop


@app.get("/api/system")
def get_system():
    """Edge-processing and host measurements (docs/edge-metrics.md)."""
    ai = explainer.state()
    return {
        "schema": "roadsense.system.v1",
        "app": {"name": "RoadSense", "version": __version__, "started_at_unix_s": round(started[0], 3),
                "uptime_s": round(time.monotonic() - started[1], 1)},
        "server_time_unix_s": round(time.time(), 3),
        "edge": dict(live_hub.metrics.summary(),
                     clients={"websocket": live_hub.client_count, "dropped_messages": live_hub.dropped}),
        "host": host.sample(),
        "safety": SAFETY,
        "ai": {"enabled": ai["enabled"], "model": ai["model"], "availability": ai["availability"],
               "status": ai["status"], "latency_ms": ai["stats"]["latency_ms"]},
    }


@app.get("/api/ai-explanation")
def get_ai_explanation():
    """The explanation of the latest safety decision and how it was made (docs/ai-explanations.md)."""
    return explainer.state()


@app.get("/api/hazards")
def get_hazards():
    """The hazard map of the latest state as roadsense.hazard.v1 records (docs/hazard-records.md)."""
    latest = live_hub.latest_json
    return hazard_records.collection(json.loads(latest) if latest else None)


# --- The dashboard


class Static(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(NO_CACHE)
        return response


app.mount("/static", Static(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_dashboard():
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)


@app.get("/autonomy")
def redirect_autonomy():
    """The 3D page's old address: it is part of the dashboard now."""
    return RedirectResponse("/", status_code=307)


def run_web(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True):
    import uvicorn

    # 0.0.0.0 means "every interface", not an address a browser can open; this computer is 127.0.0.1
    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"
    print(f"[RoadSense] Edge server and dashboard running at: {url}", flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print(f"[RoadSense] Listening on {host}: other devices on the network can open "
              f"http://<this computer's IP address>:{port}. There is no login, so anyone who can "
              "reach it can use RoadSense.", flush=True)

    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_web()
