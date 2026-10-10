"""A tiny in-process SACP server that behaves like a Snapmaker Artisan."""

from __future__ import annotations

import asyncio
import struct

from custom_components.snapmaker_artisan.sacp import (
    ATTR_ACK,
    ATTR_REQUEST,
    CMD_BED_INFO,
    CMD_CONNECT,
    CMD_CURRENT_LINE,
    CMD_DISCONNECT,
    CMD_HEARTBEAT,
    CMD_NOZZLE_INFO,
    CMD_PRINT_TIME,
    CMD_PRINTING_FILE_INFO,
    CMD_SUBSCRIBE,
    Packet,
    sacp_string,
)


class FakePrinter:
    def __init__(self) -> None:
        self.status = 0
        self.current_line = 0
        self.total_lines = 0
        self.elapsed = 0
        self.estimate = 0
        self.file_name = ""
        self.accept_connection = True
        self.subscriptions: dict[tuple[int, int], int] = {}
        self.received: list[Packet] = []
        self.server: asyncio.Server | None = None
        self.writers: list[asyncio.StreamWriter] = []
        self._seq = 1000

    @property
    def port(self) -> int:
        assert self.server
        return self.server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        for writer in self.writers:
            writer.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        try:
            while True:
                head = await reader.readexactly(4)
                (length,) = struct.unpack_from("<H", head, 2)
                packet = Packet.decode(head + await reader.readexactly(length + 3))
                self.received.append(packet)
                await self._respond(packet, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def _reply(self, writer, request: Packet, payload: bytes) -> None:
        writer.write(
            Packet(0, request.receiver_id, ATTR_ACK, request.sequence,
                   request.command_set, request.command_id, payload).encode()
        )

    async def _respond(self, packet: Packet, writer: asyncio.StreamWriter) -> None:
        if packet.command == CMD_CONNECT:
            if self.accept_connection:
                self._reply(writer, packet, b"\x00")
            # else: never answer, like an unconfirmed touchscreen prompt
        elif packet.command == CMD_SUBSCRIBE:
            command = (packet.payload[0], packet.payload[1])
            self.subscriptions[command] = struct.unpack_from("<H", packet.payload, 2)[0]
            self._reply(writer, packet, b"\x00")
        elif packet.command == CMD_PRINTING_FILE_INFO:
            if self.total_lines:
                self._reply(
                    writer, packet,
                    b"\x00" + sacp_string(self.file_name)
                    + struct.pack("<II", self.total_lines, self.estimate),
                )
            else:
                self._reply(writer, packet, b"\x01")
        elif packet.command == CMD_DISCONNECT:
            pass
        await writer.drain()

    def push_all(self) -> None:
        """Send one round of subscription data to every client."""
        payloads = {
            CMD_HEARTBEAT: bytes([0, self.status]),
            CMD_CURRENT_LINE: b"\x00" + struct.pack("<I", self.current_line),
            CMD_PRINT_TIME: b"\x00" + struct.pack("<I", self.elapsed),
            # dual nozzle: key 0, count 2, 17-byte records
            CMD_NOZZLE_INFO: b"\x00\x00\x02"
            + bytes([0, 1, 0, 1, 0]) + struct.pack("<iii", 400, 210_500, 210_000)
            + bytes([1, 1, 0, 0, 0]) + struct.pack("<iii", 400, 25_000, 0),
            CMD_BED_INFO: b"\x00\x00\x01" + bytes([0]) + struct.pack("<ih", 59_800, 60),
        }
        for writer in self.writers:
            if writer.is_closing():
                continue
            for command, payload in payloads.items():
                if command in self.subscriptions:
                    self._seq += 1
                    writer.write(Packet(0, 1, ATTR_REQUEST, self._seq, *command, payload).encode())

    def request_disconnect(self) -> None:
        """Simulate the user tapping "disconnect" on the touchscreen."""
        for writer in self.writers:
            self._seq += 1
            writer.write(Packet(0, 2, ATTR_REQUEST, self._seq, *CMD_DISCONNECT, b"").encode())
