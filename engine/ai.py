"""The coach's AI: your own Claude, never ours.

Three ways, picked by the "ai" setting ("auto" tries them in this order):
- claude_code: the `claude` command line (Claude Code) on this Mac, on your own Claude subscription.
  One process per call, fed turn by turn: 2-3 s a turn, where a new process per turn took 10-20 s.
  Locked down: no tools, no MCP servers, no settings or hooks, no saved session, empty working folder.
- api: your own Anthropic API key, from the Keychain (service "Sales Coach", account "anthropic")
  or ANTHROPIC_API_KEY. Paid per use on your Anthropic account.
- off: no AI. The talk meter, the clock, the live transcript and the call log still work.
Only transcript text and your call plan are sent; audio never leaves the Mac.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

CUE_MODEL = os.environ.get("SALES_COACH_MODEL", "claude-haiku-5-5")
_CLI_PATHS = ("/opt/homebrew/bin/claude", "/usr/local/bin/claude", "~/.claude/local/claude", "~/.local/bin/claude")
KEYCHAIN_SERVICE = "Sales Coach"


def claude_bin() -> str | None:
    for p in (os.environ.get("CLAUDE_BIN"), shutil.which("claude"), *_CLI_PATHS):
        if p and os.access(os.path.expanduser(p), os.X_OK):
            return os.path.expanduser(p)
    return None


def api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "anthropic", "-w"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def save_api_key(key: str) -> None:
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", "anthropic", "-w", key],
                   check=True, capture_output=True, timeout=5)


def backend(setting: str = "auto") -> str:
    """claude_code, api or off."""
    if setting == "off":
        return "off"
    if setting in ("auto", "claude_code") and claude_bin():
        return "claude_code"
    if setting in ("auto", "api") and api_key():
        return "api"
    return "off"


def _json_from(text: str) -> dict:
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


def _api_client():
    import anthropic
    return anthropic.Anthropic(api_key=api_key())


def _api_create(client, system: str, messages: list[dict], schema: dict, model: str, max_tokens: int):
    fmt = {"format": {"type": "json_schema", "schema": schema}, "effort": "low"}
    try:
        return client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages,
                                      output_config=fmt)
    except TypeError:   # an older anthropic package without the output_config argument
        return client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages,
                                      extra_body={"output_config": fmt})


def _api_text(resp) -> str:
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError("Claude declined this request")
    return next((b.text for b in resp.content if b.type == "text"), "")


def ask_json(system: str, user: str, schema: dict, how: str, model: str = CUE_MODEL, max_tokens: int = 4000) -> dict:
    """One question, one JSON answer that fits the schema."""
    if how == "claude_code":
        cmd = [claude_bin(), "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
               "--no-session-persistence", "--disable-slash-commands", "--model", model, "--system-prompt", system,
               "--json-schema", json.dumps(schema)]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}   # the subscription, not API billing
        with tempfile.TemporaryDirectory(prefix="coach_") as cwd:
            proc = subprocess.run(cmd, input=user, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
        try:
            out = json.loads(proc.stdout)
        except ValueError:
            raise RuntimeError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout or '')[-300:]}") from None
        if out.get("is_error") or out.get("subtype") != "success":
            raise RuntimeError(str(out.get("result") or out.get("subtype"))[:300])
        return out["structured_output"] if out.get("structured_output") is not None else _json_from(out.get("result") or "")
    if how == "api":
        resp = _api_create(_api_client(), system, [{"role": "user", "content": user}], schema, model, max_tokens)
        return _json_from(_api_text(resp))
    raise RuntimeError("AI is off")


class Conversation:
    """One conversation for the whole call: each turn sends only what is new, Claude remembers the rest."""

    def __init__(self, system: str, schema: dict, how: str, log_dir: Path, model: str = CUE_MODEL, effort: str | None = None):
        self.how, self.schema, self.system, self.model = how, schema, system, model
        if how == "api":
            self.client = _api_client()
            self.messages: list[dict] = []
            return
        if how != "claude_code":
            raise RuntimeError("AI is off")
        cmd = [claude_bin(), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
               "--disable-slash-commands", "--model", model, "--system-prompt", system,
               "--json-schema", json.dumps(schema)] + (["--effort", effort] if effort else [])
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        # a session that dies mid-call says why here
        self.err_path = log_dir / "claude_session.err"
        self.err = open(self.err_path, "a")
        self.err.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} session start\n")
        self.err.flush()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err,
                                     text=True, env=env, cwd=tempfile.mkdtemp(prefix="coach_"))
        self.out: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.out.put(line)
        self.out.put(None)

    def ask(self, text: str, timeout: float = 45) -> dict:
        if self.how == "api":
            self.messages.append({"role": "user", "content": text})
            answer = _api_text(_api_create(self.client, self.system, self.messages, self.schema, self.model, 4000))
            self.messages.append({"role": "assistant", "content": answer})
            return _json_from(answer)
        self.proc.stdin.write(json.dumps({"type": "user", "message": {
            "role": "user", "content": [{"type": "text", "text": text}]}}) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while True:
            line = self.out.get(timeout=max(0.1, deadline - time.time()))
            if line is None:
                tail = ""
                try:
                    tail = " | ".join(self.err_path.read_text(errors="ignore").strip().splitlines()[-3:])[-300:]
                except OSError:
                    pass
                raise RuntimeError(f"the Claude session ended (exit {self.proc.poll()}): {tail}")
            ev = json.loads(line)
            if ev.get("type") != "result":
                continue
            if ev.get("is_error") or ev.get("subtype") != "success":
                raise RuntimeError(str(ev.get("result") or ev.get("subtype"))[:200])
            out = ev.get("structured_output")
            return out if out is not None else _json_from(ev.get("result") or "")

    def close(self) -> None:
        if self.how != "claude_code":
            return
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.err.close()
        except Exception:  # noqa: BLE001
            pass
