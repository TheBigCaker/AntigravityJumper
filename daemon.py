"""
CODAI DAEMON — Main Entry Point
Ties together: Scheduler + IDE Bridge + Relay Client

Usage:
  python daemon.py               # uses config.json in same directory
  python daemon.py --config path/to/config.json
  python daemon.py --relay-only  # just connect relay, no scheduler
  python daemon.py --no-relay    # local-only mode

The daemon:
  1. Connects to the relay server (for cross-machine sync)
  2. Runs the scheduler (fires tasks at configured times)
  3. Each scheduled task → sends prompt to Claude via IDE bridge
  4. Response is broadcast over relay to all connected machines
  5. Relay messages from other machines are also handled
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime

import uvicorn

# Local modules
sys.path.insert(0, str(Path(__file__).parent))
from ide_bridge import IDEBridge
from relay_client import RelayClient
from scheduler import Scheduler, ScheduledTask
import web_ui

log = logging.getLogger("daemon")

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config(path: Path) -> dict:
    if not path.exists():
        log.warning(f"Config not found at {path}. Using defaults.")
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class CodaiDaemon:
    def __init__(self, config: dict, no_relay: bool = False, relay_only: bool = False):
        self.config = config
        self.no_relay = no_relay
        self.relay_only = relay_only

        machine_cfg = config.get("machine", {})
        self.machine_id = machine_cfg.get("id", os.getenv("CODAI_MACHINE_ID", "machine"))
        self.machine_role = machine_cfg.get("role", "client")  # "server" or "client"

        relay_cfg = config.get("relay", {})
        self.relay_url = relay_cfg.get("url", os.getenv("RELAY_URL", "ws://localhost:8765"))
        self.relay_secret = relay_cfg.get("secret", os.getenv("RELAY_SECRET", ""))

        bridge_cfg = config.get("bridge", {})
        self.bridge = IDEBridge(
            working_dir=bridge_cfg.get("working_dir", str(Path(__file__).parent.parent)),
            timeout=bridge_cfg.get("timeout", 180),
        )

        self.relay: RelayClient = None
        self.scheduler: Scheduler = None

    async def start(self):
        log.info(f"CODAI Daemon starting — machine: {self.machine_id} ({self.machine_role})")

        tasks = []

        # ── Web UI ────────────────────────────────────────────────────────────
        web_cfg = self.config.get("web_ui", {})
        web_port = web_cfg.get("port", 8080)
        web_host = web_cfg.get("host", "0.0.0.0")
        web_ui._scheduler_ref = None  # will be set below
        log.info(f"Web UI: http://localhost:{web_port}")
        server_config = uvicorn.Config(web_ui.app, host=web_host, port=web_port, log_level="warning")
        server = uvicorn.Server(server_config)
        tasks.append(asyncio.create_task(server.serve(), name="web_ui"))

        # ── Relay ─────────────────────────────────────────────────────────────
        if not self.no_relay:
            self.relay = RelayClient(
                machine_id=self.machine_id,
                relay_url=self.relay_url,
                secret=self.relay_secret,
            )
            self._register_relay_handlers()
            tasks.append(asyncio.create_task(self.relay.connect(), name="relay"))
            log.info(f"Relay: {self.relay_url} (as '{self.machine_id}')")

        if self.relay_only:
            log.info("relay-only mode — no scheduler")
            await asyncio.gather(*tasks)
            return

        # ── Scheduler ─────────────────────────────────────────────────────────
        self.scheduler = Scheduler(on_task=self._on_scheduled_task)
        schedule_cfg = self.config.get("schedule", [])
        if schedule_cfg:
            self.scheduler.load(schedule_cfg)
        web_ui._scheduler_ref = self.scheduler  # expose to web UI
        tasks.append(asyncio.create_task(self.scheduler.run(), name="scheduler"))

        # ── IDE Bridge inbox watcher ───────────────────────────────────────────
        tasks.append(asyncio.create_task(self.bridge.watch_inbox(), name="inbox_watcher"))

        log.info("All systems up. Running...")
        await asyncio.gather(*tasks)

    def _register_relay_handlers(self):
        relay = self.relay

        async def on_system(msg):
            event = msg.get("event")
            if event == "welcome":
                peers = [c for c in msg.get("clients", []) if c != self.machine_id]
                log.info(f"Relay connected. Peers: {peers}")
            elif event == "join":
                log.info(f"Peer joined: {msg.get('client')}")
            elif event == "leave":
                log.info(f"Peer left: {msg.get('client')}")

        async def on_task_msg(msg):
            """Handle remote task requests from other machines."""
            payload = msg.get("payload", {})
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {"prompt": payload}

            prompt = payload.get("prompt")
            task_id = payload.get("task_id", f"remote_{int(time.time()*1000)}")
            sender = msg.get("from", "unknown")

            if not prompt:
                return

            log.info(f"Remote task from '{sender}': {task_id}")
            response = await self.bridge.ask(prompt, prompt_id=task_id)

            # Send response back to sender
            await relay.send(
                {
                    "task_id": task_id,
                    "response": response,
                    "from_machine": self.machine_id,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
                to=sender,
                msg_type="task_response",
            )

        async def on_task_response(msg):
            payload = msg.get("payload", {})
            task_id = payload.get("task_id", "?")
            response = payload.get("response", "")
            from_machine = payload.get("from_machine", msg.get("from", "?"))
            log.info(f"Response from '{from_machine}' [{task_id}]:\n{response[:300]}")

            # Write to outbox so local processes can read it
            from ide_bridge import OUTBOX_DIR
            out = OUTBOX_DIR / f"{task_id}.response"
            out.write_text(response, encoding="utf-8")

        async def on_message(msg):
            payload = msg.get("payload", "")
            sender = msg.get("from", "?")
            log.info(f"[{sender}] {payload}")

        relay.on("system", on_system)
        relay.on("task", on_task_msg)
        relay.on("task_response", on_task_response)
        relay.on("message", on_message)

    async def _on_scheduled_task(self, task: ScheduledTask):
        """Called by scheduler when a task fires."""
        log.info(f"Scheduled task: {task.id}")

        if not task.prompt:
            log.warning(f"Task '{task.id}' has no prompt — skipping")
            return

        # Run locally via IDE bridge
        response = await self.bridge.ask(task.prompt, prompt_id=task.id)

        # Broadcast response over relay if connected
        if self.relay and self.relay.is_connected:
            msg = {
                "task_id": task.id,
                "response": response,
                "from_machine": self.machine_id,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
            target = task.to if task.to and task.to != "*" else None
            await self.relay.send(msg, to=target, msg_type="task_response")

    async def send_task(self, prompt: str, to: str = None, task_id: str = None):
        """Manually dispatch a task to another machine or self."""
        tid = task_id or f"manual_{int(time.time()*1000)}"
        if to and to != self.machine_id:
            if self.relay and self.relay.is_connected:
                await self.relay.send({"prompt": prompt, "task_id": tid}, to=to, msg_type="task")
            else:
                log.warning("Relay not connected — running locally")
                await self.bridge.ask(prompt, prompt_id=tid)
        else:
            await self.bridge.ask(prompt, prompt_id=tid)


# ── CLI entry point ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="CODAI Daemon")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to config.json")
    parser.add_argument("--no-relay", action="store_true", help="Run without relay (local only)")
    parser.add_argument("--relay-only", action="store_true", help="Relay only, no scheduler")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                Path(__file__).parent / "logs" / "daemon.log",
                encoding="utf-8",
            ),
        ],
    )

    config = load_config(Path(args.config))
    daemon = CodaiDaemon(
        config,
        no_relay=args.no_relay,
        relay_only=args.relay_only,
    )

    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        log.info("Daemon stopped by user.")


if __name__ == "__main__":
    main()
