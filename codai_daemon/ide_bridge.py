"""
CODAI IDE BRIDGE
Interacts with the Claude Code agent window in the IDE (Google Antigravity / VS Code).

How it works:
  1. WRITE: Runs `claude --print "prompt"` in headless mode, captures response.
  2. READ:  Saves all prompts/responses to logs/ for the web UI to read.
  3. INBOX: Watches inbox/ for .prompt files — processes and writes to outbox/.

Claude Code CLI flags used:
  --print            non-interactive, print response and exit
  --output-format    text | json | stream-json
"""

import asyncio
import glob
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

log = logging.getLogger("ide_bridge")

BASE_DIR  = Path(os.getenv("CODAI_BASE", Path(__file__).parent))
INBOX_DIR  = BASE_DIR / "inbox"
OUTBOX_DIR = BASE_DIR / "outbox"
LOG_DIR    = BASE_DIR / "logs"

for d in (INBOX_DIR, OUTBOX_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _find_claude() -> Optional[str]:
    """Auto-detect the Claude CLI binary."""
    candidates = [
        "claude",
        os.path.expanduser("~/.nvm/versions/node/*/bin/claude"),
        "C:/Users/" + os.getenv("USERNAME", "user") + "/AppData/Roaming/npm/claude.cmd",
    ]
    for c in candidates:
        for hit in glob.glob(c) or [c]:
            try:
                result = subprocess.run(
                    [hit, "--version"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    return hit
            except Exception:
                pass
    return None


CLAUDE_BIN = os.getenv("CLAUDE_BIN") or _find_claude() or "claude"


class IDEBridge:
    def __init__(
        self,
        working_dir: str = str(BASE_DIR.parent),
        on_response: Optional[Callable] = None,
        timeout: int = 180,
    ):
        self.working_dir = working_dir
        self.on_response = on_response
        self.timeout = timeout
        self._watching = False

    # ── Headless invocation ──────────────────────────────────────────────────

    async def ask(self, prompt: str, prompt_id: str = None) -> str:
        """Send a prompt to Claude via headless CLI, return response text."""
        prompt_id = prompt_id or f"req_{int(time.time()*1000)}"
        log.info(f"[{prompt_id}] Sending prompt ({len(prompt)} chars)")

        # Audit log
        (LOG_DIR / f"{prompt_id}.prompt.txt").write_text(prompt, encoding="utf-8")

        try:
            proc = await asyncio.create_subprocess_exec(
                CLAUDE_BIN,
                "--print",
                "--output-format", "text",
                prompt,
                cwd=self.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                return f"[TIMEOUT after {self.timeout}s]"

            response = stdout.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0 and not response:
                err = stderr.decode("utf-8", errors="replace").strip()
                response = f"[ERROR rc={proc.returncode}] {err}"

        except FileNotFoundError:
            response = (
                f"[ERROR] Claude CLI not found at '{CLAUDE_BIN}'. "
                "Install Claude Code and set CLAUDE_BIN env var if needed."
            )

        # Save response for web UI
        resp_file = LOG_DIR / f"{prompt_id}.response.txt"
        resp_file.write_text(response, encoding="utf-8")
        log.info(f"[{prompt_id}] Response saved ({len(response)} chars)")

        if self.on_response:
            try:
                if asyncio.iscoroutinefunction(self.on_response):
                    await self.on_response(prompt_id, response)
                else:
                    self.on_response(prompt_id, response)
            except Exception as e:
                log.error(f"on_response callback error: {e}")

        return response

    # ── File inbox watcher ────────────────────────────────────────────────────

    async def watch_inbox(self):
        """Poll inbox/ for .prompt files, process them, write to outbox/."""
        self._watching = True
        log.info(f"Watching inbox: {INBOX_DIR}")
        while self._watching:
            for prompt_file in sorted(INBOX_DIR.glob("*.prompt")):
                try:
                    prompt_id = prompt_file.stem
                    prompt_text = prompt_file.read_text(encoding="utf-8").strip()
                    prompt_file.unlink()
                    log.info(f"Inbox: processing {prompt_id}")
                    response = await self.ask(prompt_text, prompt_id=prompt_id)
                    (OUTBOX_DIR / f"{prompt_id}.response").write_text(response, encoding="utf-8")
                except Exception as e:
                    log.error(f"Inbox error for {prompt_file}: {e}")
            await asyncio.sleep(1)

    def stop_watch(self):
        self._watching = False

    # ── Convenience helpers ────────────────────────────────────────────────────

    @staticmethod
    def drop_prompt(text: str, prompt_id: str = None) -> Path:
        pid = prompt_id or f"drop_{int(time.time()*1000)}"
        p = INBOX_DIR / f"{pid}.prompt"
        p.write_text(text, encoding="utf-8")
        return p

    @staticmethod
    def read_response(prompt_id: str, timeout: float = 120) -> Optional[str]:
        resp_file = OUTBOX_DIR / f"{prompt_id}.response"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if resp_file.exists():
                text = resp_file.read_text(encoding="utf-8")
                resp_file.unlink()
                return text
            time.sleep(0.5)
        return None

    @staticmethod
    def get_recent_responses(n: int = 10) -> list:
        """Return last n responses for web UI display."""
        files = sorted(
            LOG_DIR.glob("*.response.txt"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        results = []
        for f in files[:n]:
            prompt_file = LOG_DIR / f.name.replace(".response.txt", ".prompt.txt")
            results.append({
                "id": f.stem.replace(".response", ""),
                "prompt": prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else "",
                "response": f.read_text(encoding="utf-8"),
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
        return results


# ── Standalone test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main():
        bridge = IDEBridge()
        prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "List files in current directory."
        print(f"\nSending: {prompt}\n{'─'*60}")
        response = await bridge.ask(prompt)
        print(response)

    asyncio.run(main())
