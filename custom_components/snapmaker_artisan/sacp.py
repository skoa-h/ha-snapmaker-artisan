"""Minimal asyncio client for the Snapmaker SACP protocol (Artisan / J1).

The Artisan does not expose the HTTP API (port 8080) of the Snapmaker 2.0
A-series. Status is only available through SACP, a binary protocol spoken
over TCP port 8888, the same protocol Snapmaker Luban uses.

Packet layout (all multi-byte values little endian)::

    0   0xAA 0x55          start of frame
    2   uint16 length      payload length + 8
    4   0x01               protocol version
    5   receiver id        1 = controller, 2 = touchscreen
    6   crc8 over bytes 0..5
    7   sender id          0 = us
    8   attribute          0 = request, 1 = ack/response
    9   uint16 sequence
    11  command set
    12  command id
    13  payload
    -2  uint16 checksum over bytes 7..end of payload

This module has no Home Assistant dependency so it can be tested standalone.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum

_LOGGER = logging.getLogger(__name__)

SACP_PORT = 8888
DISCOVERY_PORT = 20054

PEER_US = 0
PEER_CONTROLLER = 1
PEER_SCREEN = 2

ATTR_REQUEST = 0
ATTR_ACK = 1

HEADER_LEN = 13

# (command set, command id)
CMD_SUBSCRIBE = (0x01, 0x00)
CMD_CONNECT = (0x01, 0x05)
CMD_DISCONNECT = (0x01, 0x06)
CMD_HEARTBEAT = (0x01, 0xA0)
CMD_NOZZLE_INFO = (0x10, 0xA0)
CMD_BED_INFO = (0x14, 0xA0)
CMD_FILE_INFO = (0xAC, 0x00)
CMD_PRINTING_FILE_INFO = (0xAC, 0x1A)
CMD_CURRENT_LINE = (0xAC, 0xA0)
CMD_PRINT_TIME = (0xAC, 0xA5)
CMD_SCREEN_HEARTBEAT = (0xB0, 0x0B)

# The touchscreen often never answers the printing file info request (e.g. for
# prints started on the touchscreen), so don't keep asking every few seconds.
FILE_INFO_RETRY = 60
FILE_INFO_TIMEOUT = 5


class SacpError(Exception):
    """Base error."""


class SacpConnectionError(SacpError):
    """Printer unreachable or connection lost."""


class SacpAuthError(SacpError):
    """Connection was not confirmed on the touchscreen."""


class MachineStatus(IntEnum):
    """Workflow status reported by the heartbeat."""

    IDLE = 0
    STARTING = 1
    PRINTING = 2
    PAUSING = 3
    PAUSED = 4
    STOPPING = 5
    STOPPED = 6
    FINISHING = 7
    COMPLETED = 8
    RECOVERING = 9
    RESUMING = 10


# States in which a job is loaded and progress is meaningful.
ACTIVE_STATUSES = {
    MachineStatus.STARTING,
    MachineStatus.PRINTING,
    MachineStatus.PAUSING,
    MachineStatus.PAUSED,
    MachineStatus.FINISHING,
    MachineStatus.RECOVERING,
    MachineStatus.RESUMING,
}


def crc8(data: bytes) -> int:
    """CRC-8 (poly 0x07) used for the header."""
    crc = 0
    for byte in data:
        for bit in range(8):
            if ((crc >> 7) & 1) != ((byte >> (7 - bit)) & 1):
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def checksum16(data: bytes) -> int:
    """Internet-style checksum (big-endian words) used for the body."""
    total = 0
    for i in range(0, len(data) - 1, 2):
        total += (data[i] << 8) | data[i + 1]
    if len(data) % 2:
        total += data[-1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


@dataclass(slots=True)
class Packet:
    """A decoded SACP packet."""

    receiver_id: int
    sender_id: int
    attribute: int
    sequence: int
    command_set: int
    command_id: int
    payload: bytes = b""

    @property
    def command(self) -> tuple[int, int]:
        return (self.command_set, self.command_id)

    def encode(self) -> bytes:
        head = struct.pack("<BBHBB", 0xAA, 0x55, len(self.payload) + 8, 0x01, self.receiver_id)
        body = (
            struct.pack(
                "<BBHBB",
                self.sender_id,
                self.attribute,
                self.sequence,
                self.command_set,
                self.command_id,
            )
            + self.payload
        )
        return head + bytes([crc8(head)]) + body + struct.pack("<H", checksum16(body))

    @classmethod
    def decode(cls, frame: bytes) -> Packet:
        if len(frame) < HEADER_LEN + 2 or frame[0] != 0xAA or frame[1] != 0x55:
            raise SacpError("not a SACP frame")
        (length,) = struct.unpack_from("<H", frame, 2)
        if length + 7 != len(frame):
            raise SacpError("length mismatch")
        if crc8(frame[:6]) != frame[6]:
            raise SacpError("header CRC mismatch")
        body = frame[7:-2]
        if checksum16(body) != struct.unpack_from("<H", frame, len(frame) - 2)[0]:
            raise SacpError("checksum mismatch")
        sender, attribute, sequence, cmd_set, cmd_id = struct.unpack_from("<BBHBB", frame, 7)
        return cls(frame[5], sender, attribute, sequence, cmd_set, cmd_id, frame[HEADER_LEN:-2])


def sacp_string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<H", len(raw)) + raw


def read_sacp_string(data: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<H", data, offset)
    start = offset + 2
    return data[start : start + length].decode(errors="replace"), start + length


@dataclass
class Extruder:
    """Temperatures of one nozzle (°C)."""

    current: float
    target: float


@dataclass
class PrinterState:
    """Everything we know about the printer, updated by pushed packets."""

    status: MachineStatus | None = None
    file_name: str | None = None
    total_lines: int = 0
    current_line: int = 0
    estimated_time: int = 0  # seconds, slicer estimate for the whole job
    elapsed_time: int = 0  # seconds
    extruders: dict[int, Extruder] = field(default_factory=dict)
    bed_current: float | None = None
    bed_target: float | None = None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def progress(self) -> float | None:
        """Print progress in percent."""
        if self.status == MachineStatus.COMPLETED:
            return 100.0
        if not self.is_active:
            return 0.0
        if not self.total_lines:
            return None
        return round(min(self.current_line / self.total_lines, 1.0) * 100, 1)

    @property
    def remaining_time(self) -> int | None:
        """Remaining seconds, using the same heuristic as Snapmaker Luban."""
        if self.status == MachineStatus.COMPLETED:
            return 0
        if not self.is_active or not self.total_lines:
            return None
        fraction = min(self.current_line / self.total_lines, 1.0)
        if self.estimated_time:
            # Blend the slicer estimate (trusted early) with the observed
            # rate (trusted late), as Luban does.
            left = 1 - fraction
            return int(left * left * self.estimated_time + left * self.elapsed_time)
        if fraction <= 0:
            return None
        return int(self.elapsed_time * (1 - fraction) / fraction)


class SnapmakerClient:
    """Keeps one SACP connection open and tracks printer state."""

    def __init__(
        self,
        host: str,
        *,
        port: int = SACP_PORT,
        client_name: str = "Home Assistant",
        on_update: Callable[[PrinterState], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.client_name = client_name
        self.state = PrinterState()
        self._on_update = on_update
        self._on_disconnect = on_disconnect
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._pending: dict[tuple[int, int, int], asyncio.Future[Packet]] = {}
        self._sequence = 0
        self._last_heartbeat = 0.0
        self._file_info_task: asyncio.Task | None = None
        self._file_info_attempt = 0.0
        self._line_count_warned = False
        self._closing = False

    def set_callbacks(
        self,
        on_update: Callable[[PrinterState], None] | None,
        on_disconnect: Callable[[], None] | None,
    ) -> None:
        """Replace the update / disconnect callbacks."""
        self._on_update = on_update
        self._on_disconnect = on_disconnect

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # ------------------------------------------------------------------ setup

    async def connect(self, auth_timeout: float = 60) -> None:
        """Open the connection and subscribe to status updates.

        The first connection from a new client has to be accepted on the
        touchscreen, so the response to the connect request may take as long
        as the user needs to walk to the printer.
        """
        if self._writer or self._read_task or self._watchdog_task:
            await self.disconnect()
        self._closing = False
        # Unknown until the first heartbeat, so that heartbeat re-reads the
        # job info even if a different job started while we were away.
        self.state.status = None
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=10
            )
        except (OSError, TimeoutError) as err:
            raise SacpConnectionError(f"cannot reach {self.host}:{self.port}: {err}") from err

        self._read_task = asyncio.create_task(self._read_loop())
        try:
            payload = (
                sacp_string(self.client_name)  # host name shown on screen
                + sacp_string(self.client_name)  # client name
                + sacp_string("")  # token (unused by Artisan firmware)
            )
            try:
                response = await self._request(PEER_SCREEN, CMD_CONNECT, payload, auth_timeout)
            except TimeoutError as err:
                raise SacpAuthError("connection was not confirmed on the touchscreen") from err
            if not response.payload or response.payload[0] != 0:
                raise SacpAuthError(f"connection refused by printer ({response.payload.hex()})")

            await self._send(PEER_SCREEN, CMD_SCREEN_HEARTBEAT, b"")
            self._last_heartbeat = time.monotonic()
            for command, interval in (
                (CMD_HEARTBEAT, 2000),
                (CMD_CURRENT_LINE, 5000),
                (CMD_PRINT_TIME, 5000),
                (CMD_NOZZLE_INFO, 5000),
                (CMD_BED_INFO, 5000),
            ):
                await self._subscribe(command, interval)
        except (OSError, TimeoutError) as err:
            await self.disconnect()
            raise SacpConnectionError(f"connection setup failed: {err!r}") from err
        except BaseException:
            await self.disconnect()
            raise

        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def disconnect(self) -> None:
        """Close the connection (politely if possible)."""
        self._closing = True
        tasks = [
            t
            for t in (self._watchdog_task, self._file_info_task, self._read_task)
            if t and t is not asyncio.current_task()
        ]
        if self.connected:
            try:
                await self._send(PEER_SCREEN, CMD_DISCONNECT, b"")
            except (OSError, SacpError):
                pass
        if self._writer:
            self._writer.close()
            try:
                await asyncio.wait_for(self._writer.wait_closed(), timeout=2)
            except (OSError, TimeoutError):
                pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._fail_pending()
        self._reader = self._writer = None
        self._read_task = self._watchdog_task = self._file_info_task = None

    # -------------------------------------------------------------- transport

    def _next_sequence(self) -> int:
        self._sequence = self._sequence % 0xFFFF + 1
        return self._sequence

    async def _send(
        self, receiver: int, command: tuple[int, int], payload: bytes,
        *, attribute: int = ATTR_REQUEST, sequence: int | None = None,
    ) -> int:
        if not self._writer:
            raise SacpConnectionError("not connected")
        seq = self._next_sequence() if sequence is None else sequence
        packet = Packet(receiver, PEER_US, attribute, seq, command[0], command[1], payload)
        self._writer.write(packet.encode())
        await self._writer.drain()
        return seq

    async def _request(
        self, receiver: int, command: tuple[int, int], payload: bytes, timeout: float = 10
    ) -> Packet:
        seq = self._next_sequence()
        key = (command[0], command[1], seq)
        future: asyncio.Future[Packet] = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            await self._send(receiver, command, payload, sequence=seq)
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(key, None)

    async def _subscribe(self, command: tuple[int, int], interval_ms: int) -> None:
        payload = bytes(command) + struct.pack("<H", interval_ms)
        response = await self._request(PEER_CONTROLLER, CMD_SUBSCRIBE, payload)
        if response.payload[:1] != b"\x00":
            _LOGGER.debug("Subscription to %02x/%02x rejected: %s",
                          *command, response.payload.hex())

    async def _read_packet(self) -> Packet:
        assert self._reader
        byte = await self._reader.readexactly(1)
        while True:
            if byte != b"\xaa":
                byte = await self._reader.readexactly(1)
                continue
            byte = await self._reader.readexactly(1)
            if byte != b"\x55":
                continue  # may itself be 0xAA, the start of the next frame
            length_raw = await self._reader.readexactly(2)
            (length,) = struct.unpack("<H", length_raw)
            rest = await self._reader.readexactly(length + 3)
            try:
                return Packet.decode(b"\xaa\x55" + length_raw + rest)
            except SacpError as err:
                _LOGGER.debug("Dropping invalid frame: %s", err)
            byte = await self._reader.readexactly(1)

    async def _read_loop(self) -> None:
        try:
            while True:
                packet = await self._read_packet()
                self._dispatch(packet)
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.IncompleteReadError) as err:
            _LOGGER.debug("Connection to %s lost: %s", self.host, err)
        except Exception:  # never let a parsing bug kill the loop silently
            _LOGGER.exception("Unexpected error reading from %s", self.host)
        self._fail_pending()
        if not self._closing:
            self._connection_lost()

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(SacpConnectionError("connection closed"))
        self._pending.clear()

    def _connection_lost(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._writer:
            self._writer.close()
        if self._on_disconnect:
            self._on_disconnect()

    async def _watchdog(self) -> None:
        """Heartbeats arrive every 2 s; silence means the printer is gone."""
        while True:
            await asyncio.sleep(5)
            if time.monotonic() - self._last_heartbeat > 20:
                _LOGGER.debug("No heartbeat from %s for 20 s", self.host)
                self._connection_lost()
                return

    # ---------------------------------------------------------------- parsing

    def _dispatch(self, packet: Packet) -> None:
        key = (packet.command_set, packet.command_id, packet.sequence)
        future = self._pending.get(key)
        if future and packet.attribute == ATTR_ACK and not future.done():
            future.set_result(packet)
            return

        handler = self._handlers.get(packet.command)
        if handler is not None:
            if len(packet.payload) < 2 or packet.payload[0] != 0:
                return
            try:
                handler(self, packet.payload[1:])
            except (struct.error, ValueError, IndexError) as err:
                _LOGGER.debug("Cannot parse %02x/%02x %s: %s", *packet.command,
                              packet.payload.hex(), err)
                return
            if self._on_update:
                self._on_update(self.state)
            return

        if packet.command == CMD_DISCONNECT and packet.attribute == ATTR_REQUEST:
            # Disconnect requested from the touchscreen: acknowledge and drop.
            if self._writer and not self._writer.is_closing():
                ack = Packet(packet.sender_id, PEER_US, ATTR_ACK, packet.sequence,
                             *CMD_DISCONNECT, b"\x00")
                self._writer.write(ack.encode())
            self._connection_lost()

    def _handle_heartbeat(self, data: bytes) -> None:
        self._last_heartbeat = time.monotonic()
        try:
            status = MachineStatus(data[0])
        except ValueError:
            _LOGGER.debug("Unknown machine status %s", data[0])
            return
        previous = self.state.status
        if status in ACTIVE_STATUSES and previous not in ACTIVE_STATUSES:
            # A new job started (or we connected mid-print): reset job info.
            self.state.file_name = None
            self.state.total_lines = 0
            self.state.current_line = 0
            self.state.estimated_time = 0
            self.state.elapsed_time = 0
            self._file_info_attempt = 0.0
        self.state.status = status
        self._maybe_fetch_file_info()

    def _handle_current_line(self, data: bytes) -> None:
        (self.state.current_line,) = struct.unpack_from("<I", data, 0)
        self._maybe_fetch_file_info()

    def _handle_print_time(self, data: bytes) -> None:
        (self.state.elapsed_time,) = struct.unpack_from("<I", data, 0)

    def _handle_nozzle_info(self, data: bytes) -> None:
        # key, count, then 17-byte records:
        # index, filament status, detection, status, type,
        # int32 diameter, int32 current temp, int32 target temp (all x1000)
        key, count = data[0], data[1]
        for i in range(count):
            offset = 2 + i * 17
            index = data[offset]
            current, target = struct.unpack_from("<ii", data, offset + 9)
            # Single-nozzle packets carry the nozzle number in the key.
            nozzle = key if count == 1 else index
            self.state.extruders[nozzle] = Extruder(current / 1000, target / 1000)

    def _handle_bed_info(self, data: bytes) -> None:
        # key, count, then 7-byte zones: index, int32 temp x1000, int16 target
        if data[1] < 1:
            return
        current, target = struct.unpack_from("<ih", data, 3)
        self.state.bed_current = current / 1000
        self.state.bed_target = float(target)

    _handlers: dict[tuple[int, int], Callable[[SnapmakerClient, bytes], None]] = {
        CMD_HEARTBEAT: _handle_heartbeat,
        CMD_CURRENT_LINE: _handle_current_line,
        CMD_PRINT_TIME: _handle_print_time,
        CMD_NOZZLE_INFO: _handle_nozzle_info,
        CMD_BED_INFO: _handle_bed_info,
    }

    def _maybe_fetch_file_info(self) -> None:
        """Ask the touchscreen for file name / line count of the running job."""
        if (
            not self.state.is_active
            or (self.state.total_lines and self.state.file_name)
            or (self._file_info_task and not self._file_info_task.done())
            or time.monotonic() - self._file_info_attempt < FILE_INFO_RETRY
        ):
            return
        self._file_info_attempt = time.monotonic()
        self._file_info_task = asyncio.create_task(self._fetch_file_info())

    async def _fetch_file_info(self) -> None:
        if not self.state.total_lines:
            await self._fetch_printing_file_info()
        if not self.state.file_name:
            await self._fetch_controller_file_info()
        if self._on_update:
            self._on_update(self.state)

    async def _fetch_printing_file_info(self) -> None:
        """Line count and slicer estimate from the touchscreen (needed for progress)."""
        try:
            response = await self._request(
                PEER_SCREEN, CMD_PRINTING_FILE_INFO, b"\x00", FILE_INFO_TIMEOUT
            )
            data = response.payload
            if not data or data[0] != 0:
                raise SacpError(f"rejected ({data.hex()})")
            name, offset = read_sacp_string(data, 1)
            total, estimate = struct.unpack_from("<II", data, offset)
        except (TimeoutError, SacpError, OSError, struct.error) as err:
            self._no_line_count(repr(err))
            return
        if not total:
            self._no_line_count(f"reported 0 lines ({data.hex()})")
            return
        self.state.file_name = name or self.state.file_name
        self.state.total_lines = total
        self.state.estimated_time = estimate

    def _no_line_count(self, reason: str) -> None:
        if self._line_count_warned:
            _LOGGER.debug("Printing file info not available: %s", reason)
            return
        self._line_count_warned = True
        _LOGGER.warning(
            "Snapmaker at %s does not report the line count of the running job "
            "(%s); print progress and remaining time stay unknown. Will keep asking "
            "every %s s",
            self.host, reason, FILE_INFO_RETRY,
        )

    async def _fetch_controller_file_info(self) -> None:
        """Name of the running job from the controller; it knows even when the screen won't say."""
        try:
            response = await self._request(
                PEER_CONTROLLER, CMD_FILE_INFO, b"", FILE_INFO_TIMEOUT
            )
            data = response.payload
            if not data or data[0] != 0:
                raise SacpError(f"rejected ({data.hex()})")
            _md5, offset = read_sacp_string(data, 1)
            name, _ = read_sacp_string(data, offset)
        except (TimeoutError, SacpError, OSError, struct.error) as err:
            _LOGGER.debug("File info not available from controller: %r", err)
            return
        self.state.file_name = name or None


@dataclass
class DiscoveredPrinter:
    host: str
    name: str
    model: str


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, DiscoveredPrinter] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        # "Snapmaker J1X123P@192.168.1.201|model:Snapmaker Artisan|status:IDLE|SACP:1"
        text = data.decode(errors="replace")
        if "@" not in text or "|model:" not in text:
            return
        head, *fields = text.split("|")
        name, _, host = head.rpartition("@")
        info = dict(f.split(":", 1) for f in fields if ":" in f)
        host = host or addr[0]
        self.found[host] = DiscoveredPrinter(host, name, info.get("model", ""))


async def discover(timeout: float = 3) -> list[DiscoveredPrinter]:
    """Broadcast the Snapmaker discovery message and collect replies."""
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _DiscoveryProtocol, local_addr=("0.0.0.0", 0), allow_broadcast=True
        )
    except OSError as err:
        _LOGGER.debug("Discovery not possible: %s", err)
        return []
    try:
        transport.sendto(b"discover", ("255.255.255.255", DISCOVERY_PORT))
        await asyncio.sleep(timeout)
    except OSError as err:
        _LOGGER.debug("Discovery broadcast failed: %s", err)
    finally:
        transport.close()
    return list(protocol.found.values())
