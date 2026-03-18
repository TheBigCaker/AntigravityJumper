# CODAI Daemon

Three-part system: **Scheduler** + **IDE Bridge** + **Cross-machine Relay**

---

## Files

| File | Purpose |
|------|---------|
| `relay_server.py` | Run on **desktop** — WebSocket hub + web dashboard |
| `relay_client.py` | Connects any machine to the relay |
| `ide_bridge.py` | Sends prompts to Claude CLI, captures responses |
| `scheduler.py` | Fires tasks at specific times or intervals |
| `daemon.py` | Main process — ties everything together |
| `config.json` | Your settings (edit per-machine) |

---

## Quick Start

### Desktop (relay server + daemon)
```bash
# 1. Start relay server (leave running)
python relay_server.py
# Dashboard: http://localhost:8765/

# 2. Edit config.json: set machine.id = "desktop"
# 3. Start daemon
python daemon.py --config config.json
```

### Laptop
```bash
# 1. Edit config.json:
#    - machine.id = "laptop"
#    - relay.url = "ws://DESKTOP_IP:8765"
# 2. Start daemon
python daemon.py --config config.json
```

---

## Scheduling Tasks

Edit the `schedule` array in `config.json`:

```json
{
  "id": "my_task",
  "time": "09:00",              // daily at 9am
  "days": ["mon","wed","fri"],  // optional: specific days
  "prompt": "What should I work on today?",
  "to": "*",                    // "*" = broadcast, or "desktop"/"laptop"
  "enabled": true
}
```

Or use `interval_minutes` for repeating tasks:
```json
{
  "id": "pulse",
  "interval_minutes": 30,
  "prompt": "Quick status check.",
  "enabled": true
}
```

---

## Drop a Prompt Manually

Drop any `.prompt` file into `codai_daemon/inbox/`:
```
echo "Summarize the last training run." > inbox/my_task.prompt
```
Response appears in `outbox/my_task.response`.

---

## Cross-network Setup

If laptop and desktop are NOT on the same network:

**Option A — expose desktop directly:**
- Forward port 8765 on your router to desktop's LAN IP
- Use your public IP in `relay.url`

**Option B — Cloudflare Tunnel (recommended, free):**
```bash
# On desktop:
winget install Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8765
# It prints a public URL like: https://xxx.trycloudflare.com
# Use that in relay.url (change ws:// to wss://)
```

**Option C — ngrok:**
```bash
ngrok http 8765
# Use the wss:// URL ngrok gives you
```
