"""FastAPI Web Server for RoadSense Smart Road Monitoring System."""
from __future__ import annotations

import asyncio
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from roadsense import __version__
from roadsense.web.service import WebTelemetryManager

manager: Optional[WebTelemetryManager] = None  # one per app run, created by lifespan()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    manager = WebTelemetryManager()
    manager.set_event_loop(asyncio.get_running_loop())
    try:
        yield  # no telemetry source until the user connects the Arduino or starts the simulator
    finally:
        manager.shutdown()


# The UI is served from this app, so it is same-origin: no CORS middleware.
app = FastAPI(title="RoadSense Web Dashboard", version=__version__, lifespan=lifespan)

# Static directory path
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


# Pydantic request models
class ConnectRequest(BaseModel):
    port: str
    baud: int = 115200


class SimulatorRequest(BaseModel):
    profile: str = "normal"  # normal, bumpy, highway


class TriggerEventRequest(BaseModel):
    event_type: str = "POTHOLE"  # POTHOLE or SPEED_BREAKER


class StartRecordingRequest(BaseModel):
    name: str = ""
    notes: str = ""


# --- REST Endpoints ---


@app.get("/api/status")
def get_status():
    return manager.get_status()


@app.get("/api/ports")
def get_ports():
    return {"ports": manager.get_serial_ports()}


@app.post("/api/connect")
def connect_serial(req: ConnectRequest):
    res = manager.connect_serial(req.port, req.baud)
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res


@app.post("/api/disconnect")
def disconnect_serial():
    return manager.disconnect_serial()


@app.post("/api/simulator/start")
def start_simulator(req: SimulatorRequest = SimulatorRequest()):
    return manager.start_simulator(profile=req.profile)


@app.post("/api/simulator/stop")
def stop_simulator():
    return manager.stop_simulator()


@app.post("/api/simulator/trigger")
def trigger_sim_event(req: TriggerEventRequest):
    return manager.trigger_sim_event(req.event_type)


@app.post("/api/recording/start")
def start_recording(req: StartRecordingRequest = StartRecordingRequest()):
    return manager.start_recording(req.name, req.notes)


@app.post("/api/recording/stop")
def stop_recording():
    return manager.stop_recording()


@app.get("/api/sessions")
def list_sessions():
    return {"sessions": manager.list_sessions()}


@app.get("/api/sessions/{session_id}/events")
def get_session_events(session_id: int):
    return {"events": manager.get_session_events(session_id)}


@app.get("/api/sessions/{session_id}/export/zip")
def download_session_zip(session_id: int):
    try:
        data = manager.export_session_zip(session_id)
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=roadsense_session_{session_id}.zip"},
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/diagnostics/raw")
def get_raw_lines():
    return {
        "raw_lines": manager.recent_raw_lines,
        "recent_events": manager.recent_events,
        "malformed_count": manager.malformed_line_count,
    }


# --- WebSocket Realtime Stream ---


@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    manager.register_subscriber(queue)

    async def forward() -> None:
        try:
            await websocket.send_json({
                "type": "init",
                "status": manager.get_status(),
                "raw_history": manager.recent_raw_lines,
                "recent_events": manager.recent_events,
            })
            while True:
                await websocket.send_json(await queue.get())
        except Exception:
            pass  # client gone; the receive loop below sees the disconnect

    sender = asyncio.create_task(forward())
    try:
        # The dashboard never sends anything. Listening is how a closed tab is noticed while no
        # telemetry flows; otherwise this handler (and server shutdown) waits on the queue forever.
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass
    except Exception:
        pass
    finally:
        sender.cancel()
        manager.remove_subscriber(queue)


# Serve Static UI files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>RoadSense Web Dashboard loading...</h1>")


def run_web(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True):
    import uvicorn

    url = f"http://{host}:{port}"
    print(f"[RoadSense Web] Smart Road Monitoring System Web Server running at: {url}")

    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_web()
