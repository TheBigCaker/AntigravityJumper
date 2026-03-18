"""
CODAI Web UI
A browser-based control panel served by the relay server (or standalone).

Features:
  - Live feed of all relay messages (WebSocket)
  - Schedule viewer + enable/disable/run-now buttons
  - Manual prompt sender (to any machine or self)
  - Response log viewer
  - Inbox/outbox file browser

Run standalone:  python web_ui.py
Or import and mount onto the relay server's FastAPI app.

Open in browser: http://localhost:8080
(or http://DESKTOP_IP:8080 from laptop)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))
from ide_bridge import IDEBridge, INBOX_DIR, OUTBOX_DIR, LOG_DIR, BASE_DIR

log = logging.getLogger("web_ui")

CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}

app = FastAPI(title="CODAI Control Panel")

# Live subscribers for browser WebSocket
_live_subscribers: list = []

async def push_live(event: dict):
    dead = []
    for ws in _live_subscribers:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _live_subscribers.remove(ws)

# ── HTML dashboard ────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CODAI Control Panel</title>
<style>
  :root{--bg:#0d0d0d;--panel:#161616;--border:#2a2a2a;--green:#00ff88;--dim:#888;--red:#ff4444;--yellow:#ffcc00}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:#ddd;font-family:'Consolas','Courier New',monospace;font-size:13px}
  header{background:var(--panel);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:16px}
  header h1{color:var(--green);font-size:18px;letter-spacing:2px}
  .badge{padding:3px 8px;border-radius:3px;font-size:11px;background:#222}
  .badge.online{color:var(--green);border:1px solid var(--green)}
  .badge.offline{color:var(--red);border:1px solid var(--red)}
  .layout{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:auto 1fr;gap:1px;height:calc(100vh - 50px)}
  .panel{background:var(--panel);padding:14px;border:1px solid var(--border);overflow:hidden;display:flex;flex-direction:column}
  .panel h2{color:var(--green);font-size:12px;letter-spacing:1px;margin-bottom:10px;border-bottom:1px solid var(--border);padding-bottom:6px}
  .full-width{grid-column:1/-1}
  textarea,input,select{background:#1a1a1a;color:#ddd;border:1px solid var(--border);border-radius:3px;padding:6px;font-family:inherit;font-size:12px;width:100%}
  textarea{resize:vertical;min-height:80px}
  button{background:#222;color:var(--green);border:1px solid var(--green);padding:6px 14px;cursor:pointer;font-family:inherit;font-size:12px;border-radius:3px;margin-top:4px}
  button:hover{background:var(--green);color:#000}
  button.danger{border-color:var(--red);color:var(--red)}
  button.danger:hover{background:var(--red);color:#fff}
  .log{flex:1;overflow-y:auto;background:#0a0a0a;border:1px solid var(--border);padding:8px;font-size:11px;line-height:1.7}
  .log .entry{border-bottom:1px solid #1a1a1a;padding:3px 0}
  .log .ts{color:var(--dim);margin-right:8px}
  .log .from{color:var(--yellow);margin-right:6px}
  .log .payload{color:#ccc;word-break:break-word}
  .log .system{color:var(--dim);font-style:italic}
  .log .response{color:var(--green)}
  .sched-row{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border)}
  .sched-id{color:var(--green);min-width:140px}
  .sched-next{color:var(--dim);flex:1;font-size:11px}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
  .dot.on{background:var(--green)}
  .dot.off{background:#444}
  .files{flex:1;overflow-y:auto}
  .file-entry{padding:3px 0;border-bottom:1px solid #1a1a1a;display:flex;justify-content:space-between;align-items:center}
  .file-name{color:var(--yellow);cursor:pointer}
  .file-name:hover{text-decoration:underline}
  #status-bar{position:fixed;bottom:0;left:0;right:0;background:#111;border-top:1px solid var(--border);padding:4px 12px;font-size:11px;color:var(--dim)}
</style>
</head>
<body>
<header>
  <h1>⬡ CODAI</h1>
  <span id="relay-badge" class="badge offline">relay: connecting...</span>
  <span id="peers-badge" class="badge" style="border-color:#555;color:#aaa">peers: 0</span>
  <span style="margin-left:auto;color:var(--dim)" id="clock"></span>
</header>

<div class="layout">
  <!-- Live feed -->
  <div class="panel">
    <h2>LIVE FEED</h2>
    <div class="log" id="live-log"></div>
  </div>

  <!-- Send prompt -->
  <div class="panel">
    <h2>SEND PROMPT</h2>
    <div style="display:flex;gap:6px;margin-bottom:6px">
      <select id="send-target" style="width:140px">
        <option value="">broadcast</option>
      </select>
      <input id="task-id" placeholder="task id (optional)" style="flex:1">
    </div>
    <textarea id="prompt-text" placeholder="Enter prompt for Claude...&#10;&#10;Tip: leave 'task id' blank for auto-id."></textarea>
    <div style="display:flex;gap:6px">
      <button onclick="sendPrompt()">Send via Relay</button>
      <button onclick="sendLocal()">Run Locally</button>
    </div>
    <div style="margin-top:10px">
      <h2 style="margin-bottom:6px">LAST RESPONSES</h2>
      <div class="log" id="response-log" style="max-height:160px"></div>
    </div>
  </div>

  <!-- Schedule -->
  <div class="panel">
    <h2>SCHEDULE</h2>
    <div id="sched-list" style="flex:1;overflow-y:auto"></div>
    <button onclick="reloadSchedule()" style="margin-top:8px">Refresh</button>
  </div>

  <!-- Files -->
  <div class="panel">
    <h2>INBOX / OUTBOX</h2>
    <div style="display:flex;gap:6px;margin-bottom:8px">
      <button onclick="listFiles('inbox')">Inbox</button>
      <button onclick="listFiles('outbox')">Outbox</button>
      <button onclick="listFiles('logs')">Logs</button>
    </div>
    <div class="files" id="file-list"></div>
    <div style="margin-top:8px">
      <textarea id="file-view" style="min-height:100px;font-size:11px" placeholder="Click a file to view..."></textarea>
    </div>
  </div>
</div>

<div id="status-bar">Idle</div>

<script>
const liveLog = document.getElementById('live-log');
const responseLog = document.getElementById('response-log');
const statusBar = document.getElementById('status-bar');
let ws = null;
let peers = [];

function ts() {
  return new Date().toLocaleTimeString();
}

function addEntry(container, cls, from, text, maxLines=200) {
  const div = document.createElement('div');
  div.className = 'entry';
  div.innerHTML = `<span class="ts">${ts()}</span><span class="from ${cls}">[${from}]</span><span class="payload ${cls}">${escHtml(String(text))}</span>`;
  container.prepend(div);
  // trim
  while(container.children.length > maxLines) container.lastChild.remove();
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function connectWS() {
  ws = new WebSocket(`ws://${location.host}/live`);
  ws.onopen = () => {
    document.getElementById('relay-badge').className = 'badge online';
    document.getElementById('relay-badge').textContent = 'web: connected';
    statusBar.textContent = 'Web UI connected';
  };
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    handleMsg(msg);
  };
  ws.onclose = () => {
    document.getElementById('relay-badge').className = 'badge offline';
    document.getElementById('relay-badge').textContent = 'web: reconnecting...';
    setTimeout(connectWS, 3000);
  };
}

function handleMsg(msg) {
  const type = msg.type || 'message';
  if(type === 'system') {
    addEntry(liveLog, 'system', 'system', `${msg.event}: ${JSON.stringify(msg).slice(0,120)}`);
    if(msg.clients) {
      peers = msg.clients.filter(c => c !== 'webui');
      document.getElementById('peers-badge').textContent = `peers: ${peers.length}`;
      updateTargetList();
    }
  } else if(type === 'task_response') {
    const p = msg.payload || {};
    const text = p.response || JSON.stringify(p);
    addEntry(responseLog, 'response', p.from_machine || msg.from || '?', text.slice(0,500));
    addEntry(liveLog, 'response', msg.from || '?', `[RESPONSE] ${text.slice(0,120)}`);
  } else {
    const payload = typeof msg.payload === 'object' ? JSON.stringify(msg.payload) : (msg.payload || '');
    addEntry(liveLog, '', msg.from || '?', payload.slice(0,300));
  }
}

function updateTargetList() {
  const sel = document.getElementById('send-target');
  sel.innerHTML = '<option value="">broadcast</option>';
  peers.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p; opt.textContent = p;
    sel.appendChild(opt);
  });
}

async function sendPrompt() {
  const prompt = document.getElementById('prompt-text').value.trim();
  const to = document.getElementById('send-target').value;
  const tid = document.getElementById('task-id').value.trim() || `manual_${Date.now()}`;
  if(!prompt) return;
  const res = await fetch('/api/send', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({prompt, to: to||null, task_id: tid})
  });
  const data = await res.json();
  statusBar.textContent = `Sent: ${tid} → ${to||'*'}`;
  addEntry(liveLog, '', 'you', `[SENT to ${to||'*'}] ${prompt.slice(0,100)}`);
}

async function sendLocal() {
  const prompt = document.getElementById('prompt-text').value.trim();
  const tid = document.getElementById('task-id').value.trim() || `local_${Date.now()}`;
  if(!prompt) return;
  statusBar.textContent = `Running locally... ${tid}`;
  const res = await fetch('/api/ask', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({prompt, task_id: tid})
  });
  const data = await res.json();
  addEntry(responseLog, 'response', 'local', data.response?.slice(0,500) || data.error);
  statusBar.textContent = `Done: ${tid}`;
}

async function reloadSchedule() {
  const res = await fetch('/api/schedule');
  const data = await res.json();
  const el = document.getElementById('sched-list');
  el.innerHTML = '';
  (data.tasks || []).forEach(t => {
    const row = document.createElement('div');
    row.className = 'sched-row';
    row.innerHTML = `
      <span class="dot ${t.enabled?'on':'off'}"></span>
      <span class="sched-id">${t.id}</span>
      <span class="sched-next">${t.next_run ? 'next: '+t.next_run.slice(11,16) : 'disabled'}</span>
      <button onclick="runNow('${t.id}')">Run</button>
    `;
    el.appendChild(row);
  });
  statusBar.textContent = `Schedule refreshed (${(data.tasks||[]).length} tasks)`;
}

async function runNow(taskId) {
  const res = await fetch(`/api/schedule/run/${taskId}`, {method:'POST'});
  const data = await res.json();
  statusBar.textContent = `Triggered: ${taskId}`;
}

async function listFiles(dir) {
  const res = await fetch(`/api/files/${dir}`);
  const data = await res.json();
  const el = document.getElementById('file-list');
  el.innerHTML = '';
  if(!data.files?.length) {
    el.innerHTML = '<div style="color:#555;padding:8px">empty</div>';
    return;
  }
  data.files.forEach(f => {
    const row = document.createElement('div');
    row.className = 'file-entry';
    row.innerHTML = `<span class="file-name" onclick="viewFile('${dir}','${f.name}')">${f.name}</span><span style="color:#555">${f.size}b</span>`;
    el.appendChild(row);
  });
}

async function viewFile(dir, name) {
  const res = await fetch(`/api/files/${dir}/${encodeURIComponent(name)}`);
  const data = await res.json();
  document.getElementById('file-view').value = data.content || '';
}

// Clock
setInterval(() => {
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}, 1000);

// Init
connectWS();
reloadSchedule();
listFiles('inbox');
</script>
</body>
</html>
"""


# ── API routes ────────────────────────────────────────────────────────────────

bridge = IDEBridge(
    working_dir=str(BASE_DIR.parent),
    on_response=lambda pid, resp: asyncio.create_task(push_live({
        "type": "task_response",
        "from": "local",
        "payload": {"task_id": pid, "response": resp, "from_machine": "local"},
    })),
)

_scheduler_ref = None  # set by daemon.py when mounting


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML


@app.websocket("/live")
async def live_ws(websocket: WebSocket):
    await websocket.accept()
    _live_subscribers.append(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive, ignore incoming
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _live_subscribers:
            _live_subscribers.remove(websocket)


@app.post("/api/send")
async def api_send(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    to = body.get("to")
    task_id = body.get("task_id", f"manual_{int(datetime.utcnow().timestamp()*1000)}")
    # Push to live feed
    await push_live({"type": "message", "from": "webui", "payload": f"[to={to or '*'}] {prompt[:80]}"})
    return {"ok": True, "task_id": task_id, "to": to}


@app.post("/api/ask")
async def api_ask(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    task_id = body.get("task_id", f"ask_{int(datetime.utcnow().timestamp()*1000)}")
    if not prompt:
        return JSONResponse({"error": "no prompt"}, status_code=400)
    response = await bridge.ask(prompt, prompt_id=task_id)
    return {"task_id": task_id, "response": response}


@app.get("/api/schedule")
async def api_schedule():
    if _scheduler_ref:
        return {"tasks": _scheduler_ref.list_status()}
    config = load_config()
    return {"tasks": config.get("schedule", [])}


@app.post("/api/schedule/run/{task_id}")
async def api_run_task(task_id: str):
    if _scheduler_ref and task_id in _scheduler_ref.tasks:
        task = _scheduler_ref.tasks[task_id]
        asyncio.create_task(bridge.ask(task.prompt, prompt_id=task_id))
        return {"ok": True, "task_id": task_id}
    return JSONResponse({"error": "task not found"}, status_code=404)


@app.get("/api/files/{directory}")
async def api_list_files(directory: str):
    dirs = {"inbox": INBOX_DIR, "outbox": OUTBOX_DIR, "logs": LOG_DIR}
    d = dirs.get(directory)
    if not d:
        return JSONResponse({"error": "invalid directory"}, status_code=400)
    files = [
        {"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime}
        for f in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
        if f.is_file()
    ]
    return {"files": files[:100]}


@app.get("/api/files/{directory}/{filename}")
async def api_get_file(directory: str, filename: str):
    dirs = {"inbox": INBOX_DIR, "outbox": OUTBOX_DIR, "logs": LOG_DIR}
    d = dirs.get(directory)
    if not d:
        return JSONResponse({"error": "invalid directory"}, status_code=400)
    f = d / filename
    if not f.exists() or not f.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        return {"content": f.read_text(encoding="utf-8", errors="replace")}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/responses")
async def api_responses(limit: int = 20):
    return {"responses": bridge.get_last_response(limit)}


# ── Standalone entry ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"CODAI Web UI: http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
