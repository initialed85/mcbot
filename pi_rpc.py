#!/usr/bin/env python3
"""Pi RPC client for the mcbot container.

Speaks the JSONL protocol in pi's `docs/rpc.md`: commands to the child's stdin, `response`
records and session events on stdout. One record per line, terminated on LF only.

Design notes:

* A single reader task owns stdout. Command responses are correlated by `id`; everything else
  goes to an event queue. Two readers on one pipe would race, so `ask()` never reads directly.
* One Pi process, one run at a time. `ask()` holds a lock; Discord messages queue behind it.
* `--no-session`: mcbot keeps the per-channel transcript itself and sends it as prompt context,
  so Pi's own session store stays out of the picture.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

# The configuration table, not a literal default in here. `pi_rpc` is importable on its own (the image smoke
# test imports it directly), so it resolves its own neighbour rather than relying on `bot.py` having fixed
# `sys.path` first.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_table as cfg  # noqa: E402

log = logging.getLogger("mcbot.pi")

DEFAULT_ARGS = [
    "--mode", "rpc",
    "--no-session",
    "--approve",
    "--tools", "read,bash,edit,write",
    "--no-extensions",
    "--no-mcp",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
]


class PiError(RuntimeError):
    pass


class PiTimeout(PiError):
    pass


@dataclass
class ToolCall:
    name: str
    args: dict
    result: str = ""
    is_error: bool = False


@dataclass
class PiReply:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    aborted: bool = False
    stderr_tail: str = ""


def summarise_tool(tc: ToolCall) -> str:
    """Short line for the Discord 'working on it' message edit."""
    a = tc.args or {}
    if tc.name == "bash":
        return f"running `{str(a.get('command', ''))[:90]}`"
    if tc.name in ("read", "edit", "write"):
        return f"{tc.name} `{str(a.get('path', ''))[:70]}`"
    return f"using `{tc.name}`"


class PiRpc:
    def __init__(
        self,
        cwd: str,
        provider: str = "",
        model: str = "",
        extra_args: Optional[list[str]] = None,
        on_activity: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.cwd = cwd
        # Nothing here remembers an inference provider. A default that named one was a deployment baked into a
        # library: importable code that quietly tried somebody else's endpoint.
        self.provider = provider or str(cfg.value_of("PI_PROVIDER"))
        models = cfg.value_of("PI_MODEL")
        self.model = model or (models[0] if models else "auto")
        self.extra_args = extra_args or []
        self.on_activity = on_activity or (lambda s: None)
        # Whether every ask starts a fresh Pi session. It used to be "no", and the docstring above lied:
        # `--no-session` only stops Pi writing a session to disk (PI_CODING_AGENT_SESSION_DIR), it does not
        # reset the in-memory conversation. Measured in the pod: one child pid across two asks, sessionId
        # unchanged, messageCount 5, and the second prompt correctly recalled a word from the first. So a
        # pod carried every earlier ask - including retired refusals and stale tool state - until rollout.
        # Read through the table rather than compared to "1" here: the table is where the default lives, and a
        # comparison at the call site is a second default nobody can see from the configuration document.
        self.fresh_session = bool(cfg.value_of("MCBOT_PI_FRESH_SESSION"))
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._reader: Optional[asyncio.Task] = None
        self._stderr_tail: Deque[str] = deque(maxlen=40)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self.alive:
            return
        args = ["pi", *DEFAULT_ARGS, "--provider", self.provider, "--model", self.model, *self.extra_args]
        env = dict(os.environ)
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        env.setdefault("PI_TELEMETRY", "0")
        log.info("starting pi in %s", self.cwd)
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        if self._proc is None:
            return
        # There is no `shutdown` RPC command (docs/rpc.md#shutdown): orderly shutdown is stdin EOF,
        # optionally with SIGTERM as well, and a second signal is what force-quits. Pi waits for
        # pending work, so close stdin first and be patient before escalating.
        try:
            if self._proc.stdin is not None and not self._proc.stdin.is_closing():
                self._proc.stdin.close()
                await self._proc.stdin.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("pi did not exit on stdin EOF; sending SIGTERM")
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                log.error("pi ignored SIGTERM; killing")
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
                await self._proc.wait()
        if self._reader:
            self._reader.cancel()
        self._proc = None

    async def restart(self) -> None:
        log.info("restarting pi")
        await self.stop()
        await self.start()

    def _write(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    async def _drain_stderr(self) -> None:
        proc = self._proc
        assert proc and proc.stderr
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            self._stderr_tail.append(text)
            log.debug("pi: %s", text[:300])

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                log.warning("pi stdout closed rc=%s", self._proc.returncode)
                await self._events.put({"type": "__closed__"})
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(PiError("pi closed its stdout"))
                self._pending.clear()
                return
            text = line.decode("utf-8", "replace").strip().rstrip("\r")
            if not text:
                continue
            try:
                rec = json.loads(text)
            except json.JSONDecodeError:
                log.debug("skipping non-JSON stdout: %s", text[:200])
                continue
            await self._dispatch(rec)

    async def _dispatch(self, rec: dict) -> None:
        rtype = rec.get("type")
        if rtype == "response":
            fut = self._pending.pop(rec.get("id", ""), None)
            if fut is not None and not fut.done():
                if rec.get("success"):
                    fut.set_result(rec.get("data") or {})
                else:
                    fut.set_exception(PiError(rec.get("error") or "pi command failed"))
            return
        if rtype == "extension_ui_request":
            # mcbot starts pi with --no-extensions; if something still asks, decline instead of hanging.
            self._write({"type": "extension_ui_response", "id": rec.get("id"), "cancelled": True})
            return
        await self._events.put(rec)

    # -- commands ----------------------------------------------------------

    async def command(self, cmd: dict, timeout: float = 30.0) -> dict:
        if not self.alive:
            raise PiError("pi is not running")
        cid = cmd.setdefault("id", uuid.uuid4().hex)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        try:
            self._write(cmd)
            await self._proc.stdin.drain()  # type: ignore[union-attr]
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            raise PiTimeout(f"pi did not answer {cmd.get('type')}") from exc
        except Exception:
            self._pending.pop(cid, None)
            raise

    async def steer(self, message: str) -> dict:
        """docs/rpc.md#steer: delivered after the current assistant turn finishes its tool calls, before the
        next LLM call. This is the normal way to redirect a Pi run rather than fighting it."""
        return await self.command({"type": "steer", "message": message}, timeout=10)

    async def follow_up(self, message: str) -> dict:
        """docs/rpc.md#follow_up: delivered once the agent has nothing left to do."""
        return await self.command({"type": "follow_up", "message": message}, timeout=10)

    async def clear_queue(self) -> dict:
        return await self.command({"type": "clear_queue"}, timeout=10)

    async def abort(self) -> dict:
        return await self.command({"type": "abort"}, timeout=15)

    async def ask(self, message: str, timeout: float = 300.0) -> PiReply:
        """One prompt -> the complete assistant answer.

        `timeout` is how long Pi may stay **silent**, not how long it may work. Every event it emits re-arms
        the watchdog: an agent that has to grind through a git checkout, a commit and a rollout should not be
        cut off at five minutes, but a child that has wedged must not hold the channel forever.
        """
        async with self._lock:
            await self.start()
            if self.fresh_session:
                # docs/rpc.md#new_session: "Start a fresh session". mcbot's memory of the conversation is
                # the Discord history it builds per ask, not whatever the process happens to still be holding.
                try:
                    await self.command({"type": "new_session"}, timeout=20)
                except PiError as exc:
                    log.warning("new_session failed (%s); answering on the existing session instead", exc)
            # A steer that landed a beat after the previous run settled must not be paid out to this ask.
            try:
                left = await self.clear_queue()
                stale = (left.get("steering") or []) + (left.get("followUp") or [])
                if stale:
                    log.warning("dropped %d stale queued message(s) before a new ask: %s",
                                len(stale), " | ".join(s[:80] for s in stale))
            except PiError as exc:
                log.warning("clear_queue before ask failed: %s", exc)
            # Drop anything left over from a previous run.
            while not self._events.empty():
                try:
                    self._events.get_nowait()
                except asyncio.QueueEmpty:
                    break
            await self.command({"type": "prompt", "message": message}, timeout=30)
            texts: dict[int, str] = {}
            open_calls: dict[str, ToolCall] = {}
            reply = PiReply()
            while True:
                try:
                    rec = await asyncio.wait_for(self._events.get(), timeout=timeout)
                except asyncio.TimeoutError as exc:
                    await self.command({"type": "abort"}, timeout=10)
                    raise PiTimeout(f"pi went silent for {int(timeout)}s") from exc
                if rec.get("type") == "__closed__":
                    raise PiError("pi died mid-run")
                self._consume(rec, reply, texts, open_calls)
                if rec.get("type") == "agent_settled":
                    reply.text = "\n".join(v for k, v in sorted(texts.items()) if v).strip()
                    reply.aborted = bool(rec.get("aborted"))
                    break
            reply.stderr_tail = "\n".join(list(self._stderr_tail)[-8:])
            if not reply.text and reply.tool_calls:
                reply.text = ""
            return reply

    def _consume(self, rec: dict, reply: PiReply, texts: dict, open_calls: dict) -> None:
        rtype = rec.get("type")
        if rtype == "message_update":
            ev = rec.get("assistantMessageEvent") or {}
            kind, idx = ev.get("type"), ev.get("contentIndex", 0)
            if kind == "text_delta":
                texts[idx] = texts.get(idx, "") + (ev.get("delta") or "")
            elif kind == "text_end":
                texts[idx] = ev.get("content") or texts.get(idx, "")
        elif rtype == "tool_execution_start":
            tc = ToolCall(rec.get("toolName") or "?", rec.get("args") or {})
            open_calls[rec.get("toolCallId") or uuid.uuid4().hex] = tc
            reply.tool_calls.append(tc)
            try:
                self.on_activity(summarise_tool(tc))
            except Exception:  # noqa: BLE001 - activity is best-effort
                log.debug("activity callback failed", exc_info=True)
        elif rtype == "tool_execution_end":
            tc = open_calls.pop(rec.get("toolCallId") or "", None)
            if tc is not None:
                content = (rec.get("result") or {}).get("content") or []
                tc.result = " ".join(c.get("text", "") for c in content if isinstance(c, dict))[:1500]
                tc.is_error = bool(rec.get("isError"))
