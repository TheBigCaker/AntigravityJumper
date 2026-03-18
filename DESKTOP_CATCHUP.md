# DESKTOP CLAUDE CATCH-UP BRIEF
Generated: 2026-03-18 | From: Laptop Claude session

---

## Who You Are / Context

You are Claude running on the **desktop machine** inside the Google Antigravity IDE (Google Gemini-powered IDE). Your counterpart on the laptop just built the following system and pushed it to GitHub. You need to clone it, configure it for the desktop role, and start the relay server.

**GitHub repo:** https://github.com/TheBigCaker/AntigravityJumper
**Local path (recommended):** `C:/Codai/codai_daemon/`

---

## What Was Built

A 3-part Python system called **CODAI Daemon**:

| Component | File | What it does |
|-----------|------|-------------|
| Relay Server | `relay_server.py` | WebSocket hub — **YOU run this**. Bridges messages between machines. Dashboard at `:8765` |
| Relay Client | `relay_client.py` | Connects to relay with auto-reconnect. Used by both machines |
| IDE Bridge | `ide_bridge.py` | Invokes `claude --print` headless, captures responses. File inbox/outbox system |
| Scheduler | `scheduler.py` | Fires tasks at HH:MM or every N minutes |
| Daemon | `daemon.py` | Main entry point — starts everything |
| Web UI | `web_ui.py` | Browser control panel at `:8080` — live feed, send prompts, schedule viewer |

---

## Your Setup (Desktop)

### 1. Clone the repo
```bash
cd C:/Codai
git clone https://github.com/TheBigCaker/AntigravityJumper.git codai_daemon
cd codai_daemon
```

### 2. Install dependencies
```bash
python -m pip install -r requirements.txt
```

### 3. Edit config.json
Change these two fields:
```json
{
  "machine": {
    "id": "desktop",       // <-- change from "laptop" to "desktop"
    "role": "server"
  },
  "relay": {
    "url": "ws://localhost:8765"   // relay is local on desktop
  }
}
```

### 4. Start the relay server (Terminal 1 — keep running)
```bash
python relay_server.py
```
- Relay dashboard: http://localhost:8765/
- This is the hub. The laptop connects to YOUR IP.

### 5. Start the daemon (Terminal 2)
```bash
python daemon.py
```
- Web UI: http://localhost:8080
- Open this in any browser on the network

### 6. Tell the laptop your IP
The laptop's `config.json` needs:
```json
"relay": { "url": "ws://YOUR_DESKTOP_LAN_IP:8765" }
```
Find your IP: `ipconfig` → look for IPv4 under your network adapter.

---

## How the System Works

```
LAPTOP                          DESKTOP
  daemon.py  ←─WebSocket──────→  relay_server.py
  ide_bridge                      ide_bridge
  scheduler                       scheduler
       ↓                               ↓
  claude CLI                      claude CLI
       ↓                               ↓
  inbox/outbox                    inbox/outbox
```

- **Scheduled tasks** fire on each machine per their config
- **Prompts** can be sent machine-to-machine or broadcast via the web UI
- **Responses** are routed back to the sender and saved in `logs/`
- **File inbox**: drop a `.prompt` file in `inbox/` → response appears in `outbox/`

---

## Key Directories (auto-created)

```
codai_daemon/
├── inbox/       ← drop .prompt files here for processing
├── outbox/      ← responses appear here as .response files
└── logs/        ← audit trail of all prompts + responses
```

---

## Web UI (http://localhost:8080)

- **Live Feed** — real-time relay messages
- **Send Prompt** — send to laptop, desktop, or broadcast
- **Schedule** — view/trigger scheduled tasks
- **Inbox/Outbox/Logs** — browse and view files

The web UI is accessible from any machine on the network at `http://DESKTOP_IP:8080`.

---

## Cross-Internet Access (laptop not on same network)

If the laptop is off-network (e.g. on mobile data), expose the relay with:
```bash
# On desktop — Cloudflare Tunnel (free, no account needed for temp URLs)
winget install Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8765
# Gives you: https://some-name.trycloudflare.com
# Laptop uses: wss://some-name.trycloudflare.com in config.json
```

---

## Finding Claude CLI Path

The IDE bridge calls `claude --print`. If `claude` isn't on PATH:
```bash
where claude
# or check: %APPDATA%\npm\claude.cmd
```
Set env var if needed:
```bash
set CLAUDE_BIN=C:\path\to\claude.cmd
python daemon.py
```

---

## Current Status

- [x] All code written and pushed to GitHub
- [x] Laptop daemon ready to run
- [ ] Desktop: clone + configure + start relay server  ← **YOU ARE HERE**
- [ ] Verify both machines see each other in relay dashboard
- [ ] Enable/customize scheduled tasks in config.json

---

*This file can be deleted after setup is complete.*
