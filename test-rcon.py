#!/usr/bin/env python3
"""The RCON client, tested against a server modelled on measured behaviour, not on the textbook.

What this is really guarding is one property: mcbot builds RCON command strings out of text a player typed in
game, and a command string that contains a newline is two commands. Everything else here (auth framing,
multi-packet reassembly, reconnect) exists so that a reply reaches the right player instead of half-reaching
anybody.

The modelling matters, and the first version of this file got it wrong in the same direction the client did.
It assumed the textbook end-of-response trick - send the command, then send an *empty* command on another id,
and read until that id comes back empty. Our Paper 1.21.11 server closes the connection the moment it receives
an empty payload: captured through a byte-level proxy, `list` followed by an empty flush packet returned zero
response bytes and an EOF, while `list` alone answered fine, and `rcon-cli` - which never sends an empty
packet - works against the same server. A fake server that answers the flush packet politely is how that
passed a test suite and failed a live server.

Stdlib only, so it runs in the same CI step as test-bot-policy.py with no image and no secrets.
"""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rcon as rcon_mod  # noqa: E402
from rcon import Rcon, RconAuthError, RconError, frame, parse  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    # Refusing a non-bool here is the point: `check("x", some_list, expected)` reads like an assertion and
    # passes whenever the list is non-empty. Every check in this file must be a comparison.
    if not isinstance(condition, bool):
        raise TypeError(f"check({name!r}) was given {condition!r}, not a comparison")
    line = f"{'ok  ' if condition else 'FAIL'} {name}" + (f" ({detail})" if detail and not condition else "")
    print(line)
    if not condition:
        FAILURES.append(name)


class PaperLikeServer:
    """A Source RCON server that behaves like the one mcbot actually talks to.

    Measured behaviours kept deliberately:

    * an empty EXEC payload is answered with nothing and the connection is closed (`empty_closes`, on by
      default) - this is the trap the flush trick walks into;
    * an answer arrives on the command's own id, and nothing ever says the answer is finished;
    * a successful auth echoes the request id, a failed one answers id -1;
    * `split_at` makes a long answer arrive as several packets, with `gap` between them, because a real
      server's packets are back to back but not instantaneous, and a client that ends a response too eagerly
      truncates a whitelist.
    """

    def __init__(self, password: str = "hunter2", split_at: int = 0, drop_after: int = 0,
                 empty_closes: bool = True, gap: float = 0.0) -> None:
        self.password = password
        self.split_at = split_at
        self.drop_after = drop_after
        self.empty_closes = empty_closes
        self.gap = gap
        self.commands: list[str] = []
        self.auth_attempts: list[bytes] = []
        self.empty_payloads = 0
        self.closed_for_empty = 0
        self.server: asyncio.Server | None = None
        self.host = "127.0.0.1"
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                head = await reader.readexactly(4)
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            try:
                (size,) = struct.unpack("<i", head)
                body = await reader.readexactly(size)
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            packet_id, packet_type, payload = parse(body)
            if packet_type == rcon_mod.AUTH:
                self.auth_attempts.append(payload)
                if payload == self.password.encode():
                    writer.write(frame(packet_id, rcon_mod.RESPONSE_VALUE, b""))
                else:
                    writer.write(frame(rcon_mod.AUTH_FAIL, rcon_mod.RESPONSE_VALUE, b"Bad credentials"))
                await writer.drain()
                continue
            if packet_type == rcon_mod.EXEC:
                if payload == b"":
                    self.empty_payloads += 1
                    if self.empty_closes:
                        self.closed_for_empty += 1
                        writer.close()
                        return
                    writer.write(frame(packet_id, rcon_mod.RESPONSE_VALUE, b""))
                    await writer.drain()
                    continue
                self.commands.append(payload.decode())
                answer = f"answer to: {payload.decode()}".encode()
                if self.split_at and len(answer) > self.split_at:
                    mid = len(answer) // 2
                    writer.write(frame(packet_id, rcon_mod.RESPONSE_VALUE, answer[:mid]))
                    await writer.drain()
                    if self.gap:
                        await asyncio.sleep(self.gap)
                    writer.write(frame(packet_id, rcon_mod.RESPONSE_VALUE, answer[mid:]))
                else:
                    writer.write(frame(packet_id, rcon_mod.RESPONSE_VALUE, answer))
                await writer.drain()
                # A real server drops a session whenever it likes, including right after answering, which is
                # what a pod rolling underneath a bot that stayed up looks like from the client's side.
                if self.drop_after and len(self.commands) >= self.drop_after:
                    writer.close()
                    return


async def raw_exchange(port: int, password: str, packets: list[tuple[int, int, bytes]]) -> bytes:
    """Send raw packets and read whatever comes back, for behaviour that only shows up on the wire."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(frame(1, rcon_mod.AUTH, password.encode()))
    await writer.drain()
    got = bytearray()
    try:
        for _ in range(8):
            head = await asyncio.wait_for(reader.readexactly(4), timeout=1.0)
            (size,) = struct.unpack("<i", head)
            got += head + await asyncio.wait_for(reader.readexactly(size), timeout=1.0)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        pass
    for pid, ptype, payload in packets:
        writer.write(frame(pid, ptype, payload))
    await writer.drain()
    rest = bytearray()
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
            if not chunk:
                break
            rest += chunk
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        pass
    writer.close()
    return bytes(got) + bytes(rest)


def run(coro) -> None:
    asyncio.run(coro)


async def main() -> None:
    server = PaperLikeServer()
    await server.start()
    client = Rcon(server.host, server.port, "hunter2", timeout=5.0)

    out = await client.command("list")
    check("auth then a command returns the answer", out == "answer to: list", out)
    check("the client reports itself connected", client.connected)
    check("one command sent", server.commands == ["list"], server.commands)

    # The end-of-response trick that the textbook describes is the thing this server punishes. Asserted on the
    # wire rather than in the client, so the client cannot drift back to it and still pass.
    wire_server = PaperLikeServer()
    await wire_server.start()
    wire = await raw_exchange(wire_server.port, "hunter2", [(9, rcon_mod.EXEC, b"")])
    check("an empty command packet is answered with the connection closing",
          (wire_server.closed_for_empty, wire.count(frame(9, rcon_mod.RESPONSE_VALUE, b""))) == (1, 0),
          f"closed_for_empty={wire_server.closed_for_empty} wire={wire!r}")
    check("and the client never sends one", server.empty_payloads == 0, str(server.empty_payloads))

    # A second command must reuse the session rather than re-authenticate.
    await client.command("whitelist list")
    check("a second command reuses the session", len(server.auth_attempts) == 1,
          f"{len(server.auth_attempts)} auth attempts")

    # The multi-packet case, which has no marker any more: the answer is split and the only sign it is finished
    # is that nothing further arrives. `gap` makes sure a client that waits too briefly truncates an answer.
    big = PaperLikeServer(split_at=5, gap=0.2)
    await big.start()
    big_client = Rcon(big.host, big.port, "hunter2", timeout=5.0, quiet=0.75)
    started = time.monotonic()
    out = await big_client.command("list online players please")
    elapsed = time.monotonic() - started
    check("a split answer is reassembled whole", out == "answer to: list online players please", out)
    check("with a gap between packets, so a short quiet window is not enough to truncate it",
          elapsed > 0.2, f"{elapsed:.2f}s")
    check("and it does not wait anywhere near the command timeout to decide the answer is done",
          elapsed < 2.0, f"{elapsed:.2f}s")

    # Wrong password must be an explicit auth failure, not a timeout and not a silent empty answer.
    bad_server = PaperLikeServer(password="something else")
    await bad_server.start()
    bad = Rcon(bad_server.host, bad_server.port, "wrong", timeout=5.0)
    try:
        await bad.command("list")
        check("a wrong password raises", False, "no exception")
    except RconAuthError as exc:
        check("a wrong password raises RconAuthError", "authentication failed" in str(exc), str(exc))
    check("a failed auth leaves no session", not bad.connected)

    # The injection guard: mcbot builds `msg <player> <text>` from a chat message. A payload carrying a newline
    # would be a second command on the server, so the client refuses it before it ever reaches the socket.
    injection = Rcon(server.host, server.port, "hunter2", timeout=5.0)
    before = list(server.commands)
    try:
        await injection.command("msg Notch first line\nstop the server")
        check("a newline in a command is refused", False, "it was sent")
    except RconError as exc:
        check("a newline in a command is refused before it is sent", "newline" in str(exc), str(exc))
    check("the refused command never reached the server", server.commands == before, server.commands)
    try:
        await injection.command("msg player a\x00b")
        check("a NUL in a command is refused", False, "it was sent")
    except RconError:
        check("a NUL in a command is refused", True)
    try:
        await injection.command("   ")
        check("a blank command is refused rather than sent to a server that hangs up on it", False, "it was sent")
    except RconError as exc:
        check("a blank command is refused rather than sent to a server that hangs up on it", True, str(exc))

    # A server that hangs up straight after answering. The answer is complete; a client that calls this a
    # failure retries, and a retry of `msg <player> ...` is the same reply delivered to a child twice.
    hangy = PaperLikeServer(drop_after=1)  # closes after every answer, because len(commands) keeps growing
    await hangy.start()
    hangy_client = Rcon(hangy.host, hangy.port, "hunter2", timeout=4.0, quiet=0.4)
    got = [await hangy_client.command(c) for c in ("msg Steve one", "msg Steve two", "msg Steve three")]
    check("a hangup after a complete answer is the end of the answer, not a failure",
          got == ["answer to: msg Steve one", "answer to: msg Steve two", "answer to: msg Steve three"], got)
    check("so nothing was executed twice",
          hangy.commands == ["msg Steve one", "msg Steve two", "msg Steve three"], str(hangy.commands))
    check("and each one re-authenticated, because the server really did close",
          len(hangy.auth_attempts) == 3, str(len(hangy.auth_attempts)))

    # Reconnect once: the server drops the socket after the first command, exactly like a server pod rolling
    # underneath a bot that stayed up. The second call must still answer.
    drop = PaperLikeServer(drop_after=1)
    await drop.start()
    client2 = Rcon(drop.host, drop.port, "hunter2", timeout=5.0, quiet=0.5)
    first = await client2.command("first")
    second = await client2.command("second")
    check("a dropped session reconnects and the command still lands",
          first == "answer to: first" and second == "answer to: second", f"{first!r} / {second!r}")
    check("the drop was recovered by reconnecting, not by luck", len(drop.auth_attempts) == 2,
          f"{len(drop.auth_attempts)} auth attempts")
    check("a recovered session clears its own error report", client2.last_error == "",
          repr(client2.last_error))

    # Two commands from two coroutines must not interleave their replies on one socket.
    race = PaperLikeServer()
    await race.start()
    racer = Rcon(race.host, race.port, "hunter2", timeout=5.0, quiet=0.5)
    results = await asyncio.gather(*(racer.command(f"c{i}") for i in range(6)))
    check("concurrent commands are serialised, not interleaved",
          all(results[i] == f"answer to: c{i}" for i in range(6)), results)

    # Framing, asserted byte for byte. Little-endian here is a measurement, not a recollection: a big-endian
    # auth packet against our Paper 1.21.11 server was answered by the connection closing with no reply, and
    # this is the shape a working client (rcon-cli) was seen putting on the wire.
    check("frame() is little-endian length-prefixed, with the payload padded by two NULs",
          frame(1, 2, b"hi") == struct.pack("<i", 12) + struct.pack("<ii", 1, 2) + b"hi\x00\x00",
          frame(1, 2, b"hi"))
    check("the length prefix counts everything after itself, id and type and padding included",
          struct.unpack("<i", frame(1, 3, b"pw")[:4])[0] == 4 + 4 + 2 + 2)
    check("a big-endian packet is not what this client sends",
          frame(1, 3, b"pw") != struct.pack(">i", 12) + struct.pack(">ii", 1, 3) + b"pw\x00\x00")
    check("parse() drops the two framing NULs", parse(struct.pack("<ii", 7, 0) + b"abc\x00\x00") == (7, 0, b"abc"))
    try:
        parse(b"\x01\x00")
        check("a short packet raises rather than returning garbage", False, "no exception")
    except RconError:
        check("a short packet raises rather than returning garbage", True)

    await client.close()
    await racer.close()
    for srv in (server, big, bad_server, drop, race, wire_server, hangy):
        srv.server.close()  # type: ignore[union-attr]


run(main())
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
    sys.exit(1)
print("ALL PASS")
