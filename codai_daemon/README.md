# CODAI Daemon — Work Timer & Agent Bridge

Three systems in one:
1. **Scheduler** — wakes Claude at specific times to run tasks
2. **IDE Bridge** — sends prompts to Claude Code CLI, captures responses
3. **Relay** — syncs two machines over the internet via WebSocket

---

## Quick Start

### Desktop (run the relay server)
```bash
cd codai_daemon
pip install -r requirements.txt
python relay_server.py
# Web UI: http://localhost:8765/
# Open this URL from your laptop browser too
```

### Edit config.json on each machine
```json
// Laptop:
"machine": { "id": "laptop" },
"relay": { "url": "ws://DESKTOP_IP:8765" }

// Desktop:
"machine": { "id": "desktop" },
"relay": { "url": "ws://localhost:8765" }
```

### Run the daemon on both machines
```bash
python daemon.py --config config.json
```

---

## Cross-network (laptop not on same WiFi as desktop)

**Cloudflare Tunnel (free, no port forwarding needed):**
```bash
# On desktop — install cloudflared then:
cloudflared tunnel --url http://localhost:8765
# Prints: https://xxx.trycloudflare.com
# Set in laptop config.json:
"relay": { "url": "wss://xxx.trycloudflare.com" }
```

**ngrok:**
```bash
ngrok http 8765
# Use the wss:// URL
```

---

## Schedule Tasks

Edit `schedule` in `config.json` or use the web UI Schedule tab:

```json
{
  "id": "morning_standup",
  "time": "09:00",
  "days": ["mon", "tue", "wed", "thu", "fri"],
  "prompt": "What should I focus on today? Check Codai status.",
  "to": "*",
  "enabled": true
}
```

Or repeat on an interval:
```json
{
  "id": "training_pulse",
  "interval_minutes": 30,
  "prompt": "Check GPU usage and training loss.",
  "enabled": true
}
```

---

## Web UI Features

Open `http://DESKTOP_IP:8765/` from any browser:

- **Work Timer** — start/pause/reset session timer
- **Agent Chat** — send prompts to Claude on any connected machine
- **Schedule** — add/edit/toggle scheduled tasks visually
- **Response Log** — view all Claude responses
- **Network Setup** — instructions for cross-network config

---

## File Structure

```
codai_daemon/
  relay_server.py   # Desktop: FastAPI + WebSocket hub + web UI server
  relay_client.py   # Both: connects to relay, handles messages
  ide_bridge.py     # Both: calls Claude CLI, manages inbox/outbox
  scheduler.py      # Both: fires tasks at configured times
  daemon.py         # Both: main process tying everything together
  config.json       # Per-machine config (edit machine.id + relay.url)
  requirements.txt
  static/
    index.html      # Web UI (served by relay_server.py)
  inbox/            # Drop .prompt files here for async processing
  outbox/           # Responses appear here
  logs/             # Full prompt+response audit trail
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RELAY_PORT` | `8765` | Port relay server listens on |
| `RELAY_SECRET` | `` | Optional shared auth secret |
| `RELAY_URL` | `ws://localhost:8765` | Relay URL (overrides config) |
| `CODAI_MACHINE_ID` | `machine` | Machine ID (overrides config) |
| `CLAUDE_BIN` | auto-detect | Path to Claude CLI binary |
| `CODAI_BASE` | daemon dir | Base dir for inbox/outbox/logs |
