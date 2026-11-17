"""A small Source RCON client.

mcbot uses RCON for one job: putting text into a player's Minecraft chat. Everything else it does to the
server goes through Git and kubectl, where it is reviewable. This is the one path that speaks to the running
server directly, and it is deliberately tiny: authenticate, send a command, read the reply, reconnect.

Why not `kubectl exec` into the server console? Because the server's console is its stdin, and the itzg image
already owns it; RCON is the supported way to run a command against a live server, and it authenticates
separately from the game protocol.

The protocol (Source RCON, as vanilla and Paper implement it): every packet is a little-endian int32 length,
then int32 id, int32 type, then the payload followed by two NUL bytes. Type 3 is authentication, type 2 is an
executed command, type 0 is a response value. A successful auth is answered with the *same* id; a failed one is
answered with id -1. The byte order is little-endian and that was checked, not assumed: a big-endian auth
attempt against our Paper 1.21.11 server was answered by the connection closing with no reply at all.

A response longer than 4096 bytes arrives as several packets, and the server does not say so up front. The
usual way to spot the end is to follow the command with a second, *empty* command on a different id and read
until that id comes back empty. **This server closes the connection the moment it receives an empty payload**
- measured, not folklore: a command followed by an empty flush packet produced zero response bytes and an EOF,
while the same command alone answered fine, and `rcon-cli` (which never sends one) works against it too.

So this client sends exactly one packet per command and ends a response when the server goes quiet for
`quiet` seconds. That is a time-based end rather than a marker packet, which is the honest shape of it: the
server never tells you a response is finished, and the only signal it actually gives is no more packets.
Packets are still accumulated, so a long answer that arrives as several packets is still assembled whole.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional

log = logging.getLogger("mcbot.rcon")

AUTH = 3
EXEC = 2
RESPONSE_VALUE = 0
AUTH_FAIL = -1
MAX_PACKET = 4096  # the payload size a Source server will not exceed in one packet


class RconError(RuntimeError):
    pass


class RconAuthError(RconError):
    pass


def frame(packet_id: int, packet_type: int, body: bytes) -> bytes:
    payload = struct.pack("<ii", packet_id, packet_type) + body + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def parse(data: bytes) -> tuple[int, int, bytes]:
    """Split one packet into (id, type, body). Raises rather than returning a partial read."""
    if len(data) < 8:
        raise RconError(f"short RCON packet ({len(data)} bytes)")
    packet_id, packet_type = struct.unpack("<ii", data[:8])
    body = data[8:]
    # The two trailing NULs are part of the framing, not the answer.
    if body.endswith(b"\x00\x00"):
        body = body[:-2]
    return packet_id, packet_type, body


def one_command(text: str) -> str:
    """Return `text` if it is exactly one console command, otherwise refuse it.

    Module level rather than inline in `command()` for two reasons: mcbot builds these strings out of text a
    player typed in game, so the thing that decides what may reach the server console must be assertable
    without a server; and there must be one place that decides, not a check in the class and a copy in a test.

    A newline or CR is a second command. A NUL truncates the payload at the byte the reader stops on, which
    turns "msg Steve hello\x00" into a command nobody can predict. An all-blank command is a caller bug and is
    refused for the same reason an empty payload is never sent at all: our server answers an empty command by
    closing the connection, so a blank command is not a no-op, it is a dropped session.
    """
    if "\n" in text or "\r" in text or "\x00" in text:
        raise RconError("refused an RCON command containing a newline or NUL")
    if not text.strip():
        raise RconError("refused an empty RCON command")
    return text


class Rcon:
    """One authenticated RCON session, reconnecting on demand.

    Single client, single writer: a queue serialises commands because an RCON server answers packets in the
    order they arrive, and two concurrent commands whose replies interleave is how a client ends up reading
    someone else's output.
    """

    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0,
                 max_response: int = 64_000, quiet: float = 0.75) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.max_response = max_response
        # How long `_exec` waits for the next packet before calling the answer finished. Long enough for a
        # multi-packet answer to arrive (they come back to back), short enough that a dead session is noticed
        # in about a second rather than at the command timeout.
        self.quiet = quiet
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._id = 0
        self._lock = asyncio.Lock()
        self.connected = False
        self.last_error = ""
        self.commands = 0

    def _next_id(self) -> int:
        self._id = self._id + 1 if self._id < 2_000_000_000 else 1
        return self._id

    async def _connect(self) -> None:
        await self._close()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout)
        self._reader, self._writer = reader, writer
        auth_id = self._next_id()
        writer.write(frame(auth_id, AUTH, self.password.encode()))
        await writer.drain()
        packet_id, _, body = await self._read()
        if packet_id == AUTH_FAIL:
            await self._close()
            raise RconAuthError("RCON authentication failed (wrong password, or RCON is disabled)")
        if packet_id != auth_id:
            await self._close()
            raise RconError(f"unexpected auth reply (id {packet_id}, expected {auth_id})")
        self.connected = True
        self.last_error = ""
        log.info("rcon connected to %s:%s", self.host, self.port)

    async def _read(self, timeout: Optional[float] = None) -> tuple[int, int, bytes]:
        """One packet, waiting at most `timeout` (the command timeout unless a caller asks for the quiet window)."""
        assert self._reader is not None
        head = await asyncio.wait_for(self._reader.readexactly(4), timeout=self.timeout if timeout is None else timeout)
        (size,) = struct.unpack("<i", head)
        if not 4 <= size <= MAX_PACKET * 4:
            raise RconError(f"implausible RCON packet size {size}")
        body = await asyncio.wait_for(self._reader.readexactly(size), timeout=self.timeout)
        return parse(body)

    async def _close(self) -> None:
        self.connected = False
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
            except Exception:  # noqa: BLE001 - we are already throwing the command away
                pass

    async def command(self, text: str) -> str:
        """Run one console command and return its whole output.

        Newlines are refused rather than passed through: a payload containing a newline is a second command,
        and mcbot builds these strings out of text a player typed in game. `one_command` is where that is
        decided, and it is decided before anything is opened.
        """
        text = one_command(text)
        async with self._lock:
            for attempt in (1, 2):
                try:
                    if not self.connected:
                        await self._connect()
                    return await self._exec(text)
                except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, RconError) as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    await self._close()
                    if attempt == 2 or isinstance(exc, RconAuthError):
                        raise
                    log.warning("rcon dropped (%s); reconnecting once", exc)
        raise RconError("unreachable")  # pragma: no cover - the loop above always returns or raises

    async def _exec(self, text: str) -> str:
        """One command packet out, every packet back that carries its id, until the server stops talking.

        No marker packet: see the module docstring. `one_command` refusing an empty command is what keeps the
        payload that this server punishes out of reach of everything else in the class.
        """
        assert self._writer is not None and self._reader is not None
        cmd_id = self._next_id()
        self._writer.write(frame(cmd_id, EXEC, text.encode()))
        await self._writer.drain()
        out = bytearray()
        while True:
            try:
                packet_id, _, body = await self._read(self.quiet)
            except asyncio.TimeoutError:
                break  # nothing arrived within the quiet window, so this answer is finished
            except asyncio.IncompleteReadError:
                # The server hung up mid-read. If part of the answer already arrived, that is the end of the
                # answer and not a failure: `command()` retries by re-running the command, and re-running
                # `msg <player> ...` after it already reached the console is the bot saying the same thing to
                # a child twice. Only a hangup with nothing received is a dead session.
                if not out:
                    raise
                log.info("rcon closed the connection after answering %r; taking what arrived as the answer",
                         text[:40])
                break
            if packet_id != cmd_id:
                continue  # a server-initiated packet (auth retry, async log); not ours
            out += body
            if len(out) > self.max_response:
                log.warning("rcon response for %r exceeded %s bytes; truncating", text[:40], self.max_response)
                out = out[: self.max_response]
                break
        self.commands += 1
        return out.decode("utf-8", errors="replace").strip()

    async def close(self) -> None:
        async with self._lock:
            await self._close()
