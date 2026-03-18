"""
CODAI RELAY SERVER
Run this on the desktop (home machine).
Bridges messages between any number of connected clients over WebSocket.
Clients identify by machine name + role.

Start:
    python relay_server.py

Optional env vars:
  RELAY_PORT=8765    (default 8765)
  RELAY_SECRET=xxx   (shared secret for auth, optional)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("relay")

RELAY_SECRET = os.getenv("RELAY_SECRET", "")
RELAY_PORT = int(os.getenv("RELAY_PORT", "8765"))

app = FastAPI(title="CODAI Relay Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for the web UI
import pathlib
STATIC_DIR = pathlib.Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Active connections: client_id -> WebSocket
connections: Dict[str, WebSocket] = {}
# Message history (last 200)
message_history: list = []
MAX_HISTORY = 200


def ts():
    return datetime.utcnow().isoformat() + "Z"


def record(msg: dict):
    message_history.append(msg)
    if len(message_history) > MAX_HISTORY:
        message_history.pop(0)


async def broadcast(message: dict, exclude: str = None):
    dead = []
    for cid, ws in connections.items():
        if cid == exclude:
            continue
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead.append(cid)
    for cid in dead:
        connections.pop(cid, None)


async def send_to(client_id: str, message: dict) -> bool:
    ws = connections.get(client_id)
    if not ws:
        return False
    try:
        await ws.send_text(json.dumps(message))
        return True
    except Exception:
        connections.pop(client_id, None)
        return False


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the web UI."""
    ui_file = STATIC_DIR / "index.html"
    if ui_file.exists():
        return ui_file.read_text(encoding="utf-8")
    # Fallback minimal dashboard
    clients = list(connections.keys())
    return f"""
    <html><head><title>CODAI Relay</title>
    <meta http-equiv="refresh" content="5">
    <style>body{{font-family:monospace;background:#111;color:#0f0;padding:20px}}</style>
    </head><body>
    <h2>CODAI Relay Server</h2>
    <p>Connected: {len(clients)} — {clients}</p>
    <p><a href="/ui" style="color:#0f0">Open Full Dashboard</a></p>
    </body></html>
    """


@app.get("/ui", response_class=HTMLResponse)
async def ui():
    ui_file = STATIC_DIR / "index.html"
    if ui_file.exists():
        return ui_file.read_text(encoding="utf-8")
    return HTMLResponse("<h1>UI not built yet</h1>", status_code=404)


@app.get("/clients")
async def list_clients():
    return {"clients": list(connections.keys()), "count": len(connections)}


@app.get("/history")
async def get_history(limit: int = 50):
    return {"messages": message_history[-limit:]}


@app.get("/schedule")
async def get_schedule():
    """Return current schedule from config."""
    cfg_path = pathlib.Path(__file__).parent / "config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return {"schedule": cfg.get("schedule", [])}
    return {"schedule": []}


@app.post("/schedule")
async def update_schedule(body: dict):
    """Update schedule config and broadcast to all clients."""
    cfg_path = pathlib.Path(__file__).parent / "config.json"
    cfg = {}
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["schedule"] = body.get("schedule", [])
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    await broadcast({"type": "schedule_update", "schedule": cfg["schedule"], "timestamp": ts()})
    return {"ok": True}


@app.post("/send")
async def http_send(body: dict):
    """Send a message/task via HTTP (from web UI)."""
    msg = {
        "type": body.get("type", "message"),
        "payload": body.get("payload", ""),
        "from": body.get("from", "web-ui"),
        "timestamp": ts(),
    }
    to = body.get("to")
    if to and to != "*":
        sent = await send_to(to, msg)
        return {"ok": sent, "to": to}
    else:
        await broadcast(msg)
        return {"ok": True, "to": "*"}


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()

    if RELAY_SECRET:
        try:
            auth_msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            data = json.loads(auth_msg)
            if data.get("secret") != RELAY_SECRET:
                await websocket.send_text(json.dumps({"type": "error", "msg": "auth failed"}))
                await websocket.close()
                return
        except Exception:
            await websocket.close()
            return

    connections[client_id] = websocket
    log.info(f"Client connected: {client_id} (total: {len(connections)})")

    await broadcast({
        "type": "system",
        "event": "join",
        "from": "relay",
        "client": client_id,
        "clients": list(connections.keys()),
        "timestamp": ts(),
    })
    await websocket.send_text(json.dumps({
        "type": "system",
        "event": "welcome",
        "your_id": client_id,
        "clients": list(connections.keys()),
        "history": message_history[-20:],
        "timestamp": ts(),
    }))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                msg = {"payload": raw}

            msg["from"] = client_id
            msg["timestamp"] = ts()

            target = msg.get("to")
            if target and target != "*":
                sent = await send_to(target, msg)
                if not sent:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "msg": f"client '{target}' not found",
                        "timestamp": ts(),
                    }))
            else:
                await broadcast(msg, exclude=client_id)

            record(msg)
            log.info(f"MSG {client_id} -> {target or '*'}: {str(msg.get('payload',''))[:80]}")

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning(f"Client {client_id} error: {e}")
    finally:
        connections.pop(client_id, None)
        log.info(f"Client disconnected: {client_id}")
        await broadcast({
            "type": "system",
            "event": "leave",
            "client": client_id,
            "clients": list(connections.keys()),
            "timestamp": ts(),
        })


if __name__ == "__main__":
    print(f"Starting CODAI Relay Server on port {RELAY_PORT}")
    print(f"Web UI: http://0.0.0.0:{RELAY_PORT}/")
    print(f"Auth: {'ENABLED' if RELAY_SECRET else 'disabled'}")
    uvicorn.run(app, host="0.0.0.0", port=RELAY_PORT)
