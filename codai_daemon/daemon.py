"""
CODAI DAEMON — Main Entry Point

Usage:
  python daemon.py                  # use config.json in same dir
  python daemon.py --config path    # custom config
  python daemon.py --no-relay       # local-only, no relay
  python daemon.py --relay-only     # relay connection only
  python daemon.py --serve-ui       # also serve web UI on --ui-port
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

sys.path.insert(0, str(Path(__file__).parent))
from ide_bridge import IDEBridge
from relay_client import RelayClient
from scheduler import Scheduler, ScheduledTask

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
        self.machine_role = machine_cfg.get("role", "client")

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

        self.scheduler = Scheduler(on_task=self._on_scheduled_task)
        schedule_cfg = self.config.get("schedule", [])
        if schedule_cfg:
            self.scheduler.load(schedule_cfg)
        tasks.append(asyncio.create_task(self.scheduler.run(), name="scheduler"))
        tasks.append(asyncio.create_task(self.bridge.watch_inbox(), name="inbox_watcher"))

        log.info("All systems up. Running...")
        await asyncio.gather(*tasks)

    def _register_relay_handlers(self):
        relay = self.relay
        outbox = Path(__file__).parent / "outbox"
        outbox.mkdir(exist_ok=True)

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
            (outbox / f"{task_id}.response").write_text(response, encoding="utf-8")

        async def on_message(msg):
            log.info(f"[{msg.get('from','?')}] {msg.get('payload','')}")

        relay.on("system", on_system)
        relay.on("task", on_task_msg)
        relay.on("task_response", on_task_response)
        relay.on("message", on_message)

    async def _on_scheduled_task(self, task: ScheduledTask):
        log.info(f"Scheduled task: {task.id}")

        if not task.prompt:
            log.warning(f"Task '{task.id}' has no prompt — skipping")
            return

        response = await self.bridge.ask(task.prompt, prompt_id=task.id)

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
        tid = task_id or f"manual_{int(time.time()*1000)}"
        if to and to != self.machine_id:
            if self.relay and self.relay.is_connected:
                await self.relay.send({"prompt": prompt, "task_id": tid}, to=to, msg_type="task")
            else:
                log.warning("Relay not connected — running locally")
                await self.bridge.ask(prompt, prompt_id=tid)
        else:
            await self.bridge.ask(prompt, prompt_id=tid)


def main():
    parser = argparse.ArgumentParser(description="CODAI Daemon")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--no-relay", action="store_true")
    parser.add_argument("--relay-only", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "daemon.log", encoding="utf-8"),
        ],
    )

    config = load_config(Path(args.config))
    daemon = CodaiDaemon(config, no_relay=args.no_relay, relay_only=args.relay_only)

    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        log.info("Daemon stopped by user.")


if __name__ == "__main__":
    main()
