"""
CODAI RELAY CLIENT
Run on both laptop and desktop to stay connected to the relay server.
Handles reconnection, message routing, and event callbacks.

Usage:
  from relay_client import RelayClient
  client = RelayClient("laptop", "ws://DESKTOP_IP:8765")
  client.on("task", my_handler)
  await client.connect()
"""

import asyncio
import json
import logging
import time
from typing import Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

log = logging.getLogger("relay_client")


class RelayClient:
    def __init__(
        self,
        machine_id: str,
        relay_url: str,
        secret: str = "",
        reconnect_delay: float = 5.0,
    ):
        self.machine_id = machine_id
        self.relay_url = relay_url.rstrip("/")
        # Support both ws:// and http:// base URLs
        ws_base = self.relay_url.replace("http://", "ws://").replace("https://", "wss://")
        self.ws_url = f"{ws_base}/ws/{machine_id}"
        self.secret = secret
        self.reconnect_delay = reconnect_delay
        self._ws = None
        self._running = False
        self._handlers: Dict[str, List[Callable]] = {}
        self._connected = False
        self._send_queue: asyncio.Queue = None
        self._peers: List[str] = []

    def on(self, event_type: str, handler: Callable):
        self._handlers.setdefault(event_type, []).append(handler)

    def off(self, event_type: str, handler: Callable = None):
        if handler:
            self._handlers.get(event_type, []).remove(handler)
        else:
            self._handlers.pop(event_type, None)

    async def send(self, payload, to: str = None, msg_type: str = "message"):
        msg = {"type": msg_type, "payload": payload}
        if to:
            msg["to"] = to
        if self._send_queue:
            await self._send_queue.put(json.dumps(msg))

    async def broadcast(self, payload, msg_type: str = "message"):
        await self.send(payload, to=None, msg_type=msg_type)

    async def _dispatch(self, msg: dict):
        msg_type = msg.get("type", "message")
        handlers = self._handlers.get(msg_type, []) + self._handlers.get("*", [])
        for h in handlers:
            try:
                if asyncio.iscoroutinefunction(h):
                    await h(msg)
                else:
                    h(msg)
            except Exception as e:
                log.error(f"Handler error for type '{msg_type}': {e}")

    async def _sender(self, ws):
        while True:
            data = await self._send_queue.get()
            try:
                await ws.send(data)
            except Exception as e:
                log.warning(f"Send failed: {e}")
                await self._send_queue.put(data)
                break

    async def connect(self):
        self._running = True
        self._send_queue = asyncio.Queue()

        while self._running:
            try:
                log.info(f"Connecting to relay: {self.ws_url}")
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    self._connected = True

                    if self.secret:
                        await ws.send(json.dumps({"secret": self.secret}))

                    log.info(f"Connected as '{self.machine_id}'")
                    sender_task = asyncio.create_task(self._sender(ws))

                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                msg = {"type": "raw", "payload": raw}
                            await self._dispatch(msg)
                    finally:
                        sender_task.cancel()

            except (ConnectionClosedError, ConnectionClosedOK, OSError) as e:
                log.warning(f"Disconnected: {e}")
            except Exception as e:
                log.error(f"Relay error: {e}")
            finally:
                self._connected = False
                self._ws = None

            if self._running:
                log.info(f"Reconnecting in {self.reconnect_delay}s...")
                await asyncio.sleep(self.reconnect_delay)

    def stop(self):
        self._running = False
        if self._ws:
            asyncio.create_task(self._ws.close())

    @property
    def is_connected(self):
        return self._connected

    @property
    def peers(self):
        return self._peers


# ── Standalone CLI ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="CODAI Relay Client")
    parser.add_argument("machine_id", help="This machine's ID (e.g. 'laptop' or 'desktop')")
    parser.add_argument("relay_url", help="Relay server URL e.g. ws://192.168.1.100:8765")
    parser.add_argument("--secret", default="", help="Auth secret")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    client = RelayClient(args.machine_id, args.relay_url, secret=args.secret)

    async def handle_message(msg):
        print(f"\n[{msg.get('from','?')}] {msg.get('payload','')}")

    async def handle_system(msg):
        event = msg.get("event")
        if event == "welcome":
            print(f"Connected! Peers: {msg.get('clients', [])}")
        elif event == "join":
            print(f"  >> {msg.get('client')} joined")
        elif event == "leave":
            print(f"  << {msg.get('client')} left")

    client.on("message", handle_message)
    client.on("system", handle_system)

    async def stdin_loop():
        await asyncio.sleep(1)
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, input, "> ")
            if line.strip():
                await client.broadcast(line.strip())

    async def main():
        await asyncio.gather(client.connect(), stdin_loop())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
