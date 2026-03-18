"""
CODAI RELAY SERVER
Run this on the desktop (home machine).
Bridges messages between any number of connected clients over WebSocket.
Clients identify by machine name + role.

Start: python relay_server.py
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
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("relay")

RELAY_SECRET = os.getenv("RELAY_SECRET", "")  # empty = no auth
RELAY_PORT = int(os.getenv("RELAY_PORT", "8765"))

app = FastAPI(title="CODAI Relay Server")

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
    """Send message to all connected clients except sender."""
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
        log.info(f"Removed dead connection: {cid}")


async def send_to(client_id: str, message: dict) -> bool:
    """Send message to a specific client. Returns True if sent."""
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
    """Simple status dashboard."""
    clients = list(connections.keys())
    history_html = "".join(
        f"<tr><td>{m.get('timestamp','')}</td><td>{m.get('from','?')}</td>"
        f"<td>{m.get('to','*')}</td><td style='max-width:400px;overflow:hidden'>{str(m.get('payload',''))[:200]}</td></tr>"
        for m in reversed(message_history[-50:])
    )
    return f"""
    <html><head><title>CODAI Relay</title>
    <meta http-equiv="refresh" content="5">
    <style>body{{font-family:monospace;background:#111;color:#0f0;padding:20px}}
    table{{width:100%;border-collapse:collapse}}td,th{{border:1px solid #333;padding:4px 8px;font-size:12px}}
    th{{background:#222}}.online{{color:#0f0}}.badge{{background:#333;padding:2px 6px;border-radius:4px;margin:2px}}</style>
    </head><body>
    <h2>CODAI Relay Server</h2>
    <p>Connected: {len(clients)} clients</p>
    <p>{''.join(f'<span class="badge online">{c}</span>' for c in clients)}</p>
    <hr>
    <h3>Recent Messages (last 50)</h3>
    <table><tr><th>Time</th><th>From</th><th>To</th><th>Payload</th></tr>
    {history_html}
    </table>
    </body></html>
    """


@app.get("/clients")
async def list_clients():
    return {"clients": list(connections.keys()), "count": len(connections)}


@app.get("/history")
async def get_history(limit: int = 50):
    return {"messages": message_history[-limit:]}


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()

    # Optional auth handshake
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

    join_msg = {
        "type": "system",
        "event": "join",
        "from": "relay",
        "client": client_id,
        "clients": list(connections.keys()),
        "timestamp": ts(),
    }
    await broadcast(join_msg)
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

            # Route: if 'to' field set, send to specific client, else broadcast
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
    print(f"Dashboard: http://0.0.0.0:{RELAY_PORT}/")
    print(f"Auth: {'ENABLED' if RELAY_SECRET else 'disabled (set RELAY_SECRET env var to enable)'}")
    uvicorn.run(app, host="0.0.0.0", port=RELAY_PORT)
