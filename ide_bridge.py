"""
CODAI IDE BRIDGE
Interacts with the Claude Code agent window in the IDE.

How it works:
  1. WRITE: Writes a prompt to a sidecar file, then invokes `claude -p "prompt"`
             in headless mode and captures the full response.
  2. READ:  Watches the sidecar file for responses written back by the agent.
  3. INJECT: For IDEs that support it, uses VS Code CLI to open/focus chat.

The bridge also exposes a file-drop inbox: any .prompt file dropped into
  INBOX_DIR will be picked up, sent to Claude, and the response saved as
  a .response file in OUTBOX_DIR.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

log = logging.getLogger("ide_bridge")

# Directories (created if missing)
BASE_DIR = Path(os.getenv("CODAI_BASE", "C:/Codai/codai_daemon"))
INBOX_DIR  = BASE_DIR / "inbox"
OUTBOX_DIR = BASE_DIR / "outbox"
LOG_DIR    = BASE_DIR / "logs"

for d in (INBOX_DIR, OUTBOX_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Claude CLI — auto-detect
def _find_claude() -> Optional[str]:
    candidates = [
        "claude",
        os.path.expanduser("~/.nvm/versions/node/*/bin/claude"),
        "C:/Users/" + os.getenv("USERNAME", "") + "/AppData/Roaming/npm/claude.cmd",
        "C:/Users/" + os.getenv("USERNAME", "") + "/AppData/Local/npm-cache/_npx/*/claude.cmd",
    ]
    import glob
    for c in candidates:
        for hit in glob.glob(c) or [c]:
            try:
                result = subprocess.run(
                    [hit, "--version"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    return hit
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                continue
    return None

CLAUDE_BIN = os.getenv("CLAUDE_BIN") or _find_claude() or "claude"


class IDEBridge:
    """
    Core bridge: send prompts to Claude Code CLI, collect responses.
    """

    def __init__(
        self,
        working_dir: str = str(BASE_DIR),
        on_response: Optional[Callable] = None,
        timeout: int = 120,
    ):
        self.working_dir = working_dir
        self.on_response = on_response  # callback(prompt_id, response_text)
        self.timeout = timeout
        self._watching = False

    # ── Headless invocation ──────────────────────────────────────────────────

    async def ask(self, prompt: str, prompt_id: Optional[str] = None) -> str:
        """
        Send a prompt to Claude via headless CLI.
        Returns the full text response.
        """
        prompt_id = prompt_id or f"req_{int(time.time()*1000)}"
        log.info(f"[{prompt_id}] Sending prompt ({len(prompt)} chars)")

        # Write prompt to log for audit trail
        log_file = LOG_DIR / f"{prompt_id}.prompt.txt"
        log_file.write_text(prompt, encoding="utf-8")

        response: str = ""
        try:
            proc = await asyncio.create_subprocess_exec(
                CLAUDE_BIN,
                "--print",          # headless / non-interactive
                "--output-format", "text",
                prompt,
                cwd=self.working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                raw_out, raw_err = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                return f"[TIMEOUT after {self.timeout}s]"

            response = (raw_out or b"").decode("utf-8", errors="replace").strip()
            if proc.returncode != 0 and not response:
                err = (raw_err or b"").decode("utf-8", errors="replace").strip()
                response = f"[ERROR rc={proc.returncode}] {err}"

        except FileNotFoundError:
            response = (
                f"[ERROR] Claude CLI not found at '{CLAUDE_BIN}'. "
                "Set CLAUDE_BIN env var to the correct path."
            )

        # Save response
        resp_file = LOG_DIR / f"{prompt_id}.response.txt"
        resp_file.write_text(response, encoding="utf-8")
        preview = response[:120]
        log.info(f"[{prompt_id}] Response: {preview}...")

        cb = self.on_response
        if cb is not None:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(prompt_id, response)
                else:
                    cb(prompt_id, response)
            except Exception as e:
                log.error(f"on_response callback error: {e}")

        return response

    # ── File inbox watcher ────────────────────────────────────────────────────

    async def watch_inbox(self):
        """
        Poll INBOX_DIR for .prompt files.
        When found: run ask(), write .response to OUTBOX_DIR, delete .prompt.
        """
        self._watching = True
        log.info(f"Watching inbox: {INBOX_DIR}")
        while self._watching:
            for prompt_file in sorted(INBOX_DIR.glob("*.prompt")):
                try:
                    prompt_id = prompt_file.stem
                    prompt_text = prompt_file.read_text(encoding="utf-8").strip()
                    prompt_file.unlink()  # consume immediately
                    log.info(f"Inbox: processing {prompt_id}")
                    response = await self.ask(prompt_text, prompt_id=prompt_id)
                    out_file = OUTBOX_DIR / f"{prompt_id}.response"
                    out_file.write_text(response, encoding="utf-8")
                except Exception as e:
                    log.error(f"Inbox error for {prompt_file}: {e}")
            await asyncio.sleep(1)

    def stop_watch(self):
        self._watching = False

    # ── Convenience: drop a prompt into inbox ─────────────────────────────────

    @staticmethod
    def drop_prompt(text: str, prompt_id: str = None) -> Path:
        """
        Drop a prompt into the inbox for async processing.
        Returns the path of the created file.
        """
        pid = prompt_id or f"drop_{int(time.time()*1000)}"
        p = INBOX_DIR / f"{pid}.prompt"
        p.write_text(text, encoding="utf-8")
        return p

    @staticmethod
    def read_response(prompt_id: str, timeout: float = 120) -> Optional[str]:
        """
        Blocking wait for a response in the outbox.
        Returns response text or None on timeout.
        """
        resp_file = OUTBOX_DIR / f"{prompt_id}.response"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if resp_file.exists():
                text = resp_file.read_text(encoding="utf-8")
                resp_file.unlink()
                return text
            time.sleep(0.5)
        return None

    # ── VS Code / IDE focus helpers ───────────────────────────────────────────

    @staticmethod
    def open_ide_chat():
        """Try to open the chat panel in the IDE (best-effort)."""
        # Try VS Code CLI
        for cmd in ["code", "code-insiders"]:
            try:
                result = subprocess.run(
                    [cmd, "--command", "workbench.action.chat.open"],
                    capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def get_last_response(n: int = 1) -> list:
        """Return the last n response files from the log dir."""
        files = sorted(LOG_DIR.glob("*.response.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
        results = []
        for f in files[:n]:
            results.append({
                "id": f.stem.replace(".response", ""),
                "text": f.read_text(encoding="utf-8"),
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
        return results


# ── Standalone test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main():
        bridge = IDEBridge()

        if len(sys.argv) > 1:
            prompt = " ".join(sys.argv[1:])
        else:
            prompt = "List the files in the current directory."

        print(f"\nSending: {prompt}\n{'─'*60}")
        response = await bridge.ask(prompt)
        print(response)

    asyncio.run(main())
