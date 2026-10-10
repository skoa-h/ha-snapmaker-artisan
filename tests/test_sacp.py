"""Tests for the SACP protocol client."""

import asyncio

import pytest

from custom_components.snapmaker_artisan.sacp import (
    CMD_BED_INFO,
    CMD_CURRENT_LINE,
    CMD_HEARTBEAT,
    CMD_NOZZLE_INFO,
    CMD_PRINT_TIME,
    MachineStatus,
    Packet,
    SacpAuthError,
    SacpConnectionError,
    SacpError,
    SnapmakerClient,
    sacp_string,
)

from .fake_printer import FakePrinter

# The fake printer is a real TCP server on localhost.
pytestmark = pytest.mark.usefixtures("socket_enabled")

# Generated with Snapmaker's official @snapmaker/snapmaker-sacp-sdk (Packet.toBuffer).
SDK_VECTORS = [
    (
        Packet(2, 0, 0, 1, 0x01, 0x05,
               sacp_string("Home Assistant") * 2 + sacp_string("")),
        "aa552a000102940000010001050e00486f6d6520417373697374616e740e00486f6d65"
        "20417373697374616e74000055b6",
    ),
    (Packet(1, 0, 0, 513, 0x01, 0x00, bytes([0xAC, 0xA0, 0x88, 0x13])),
     "aa550c00010127000001020100aca0881349c9"),
    (Packet(2, 0, 0, 7, 0xAC, 0x1A, b"\x00"), "aa55090001026000000700ac1a00e54c"),
    (Packet(2, 0, 1, 65535, 0xB0, 0x0B, b""), "aa5508000102760001ffffb00bf34f"),
]


@pytest.mark.parametrize(("packet", "expected"), SDK_VECTORS)
def test_encode_matches_official_sdk(packet: Packet, expected: str) -> None:
    assert packet.encode().hex() == expected
    assert Packet.decode(bytes.fromhex(expected)) == packet


def test_decode_rejects_corruption() -> None:
    frame = bytearray.fromhex(SDK_VECTORS[1][1])
    frame[14] ^= 0xFF
    with pytest.raises(SacpError):
        Packet.decode(bytes(frame))


async def _wait_for(predicate, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def printer():
    fake = FakePrinter()
    await fake.start()
    yield fake
    await fake.stop()


async def test_connect_subscribe_and_track_print(printer: FakePrinter) -> None:
    updates = []
    client = SnapmakerClient("127.0.0.1", port=printer.port, on_update=updates.append)
    await client.connect(auth_timeout=2)
    assert client.connected
    assert set(printer.subscriptions) == {
        CMD_HEARTBEAT, CMD_CURRENT_LINE, CMD_PRINT_TIME, CMD_NOZZLE_INFO, CMD_BED_INFO,
    }

    # Idle printer.
    printer.push_all()
    await _wait_for(lambda: client.state.status == MachineStatus.IDLE)
    await _wait_for(lambda: client.state.bed_current is not None)
    assert client.state.progress == 0
    assert client.state.extruders[0].current == 210.5
    assert client.state.extruders[0].target == 210
    assert client.state.extruders[1].current == 25
    assert client.state.bed_current == 59.8
    assert client.state.bed_target == 60

    # A print starts; the client asks the touchscreen for the file info.
    printer.status = MachineStatus.PRINTING
    printer.file_name = "benchy.gcode"
    printer.total_lines = 200_000
    printer.estimate = 3600
    printer.current_line = 50_000
    printer.elapsed = 900
    printer.push_all()
    await _wait_for(lambda: client.state.total_lines == 200_000)
    await _wait_for(lambda: client.state.elapsed_time == 900)
    state = client.state
    assert state.file_name == "benchy.gcode"
    assert state.progress == 25.0
    # 0.75² * 3600 + 0.75 * 900
    assert state.remaining_time == 2700
    assert updates

    printer.status = MachineStatus.COMPLETED
    printer.push_all()
    await _wait_for(lambda: client.state.status == MachineStatus.COMPLETED)
    assert client.state.progress == 100
    assert client.state.remaining_time == 0

    await client.disconnect()
    assert not client.connected


async def test_file_info_unavailable_gives_unknown_progress(printer: FakePrinter) -> None:
    client = SnapmakerClient("127.0.0.1", port=printer.port)
    await client.connect(auth_timeout=2)
    printer.status = MachineStatus.PRINTING
    printer.current_line = 10
    printer.push_all()
    await _wait_for(lambda: client.state.current_line == 10)
    await asyncio.sleep(0.1)
    assert client.state.progress is None
    assert client.state.remaining_time is None
    await client.disconnect()


async def test_not_confirmed_on_touchscreen(printer: FakePrinter) -> None:
    printer.accept_connection = False
    client = SnapmakerClient("127.0.0.1", port=printer.port)
    with pytest.raises(SacpAuthError):
        await client.connect(auth_timeout=0.2)
    assert not client.connected


async def test_unreachable() -> None:
    client = SnapmakerClient("127.0.0.1", port=1)
    with pytest.raises(SacpConnectionError):
        await client.connect()


async def test_disconnect_from_touchscreen(printer: FakePrinter) -> None:
    lost = asyncio.Event()
    client = SnapmakerClient("127.0.0.1", port=printer.port, on_disconnect=lost.set)
    await client.connect(auth_timeout=2)
    printer.request_disconnect()
    async with asyncio.timeout(2):
        await lost.wait()
    await client.disconnect()


async def test_resync_after_garbage(printer: FakePrinter) -> None:
    client = SnapmakerClient("127.0.0.1", port=printer.port)
    await client.connect(auth_timeout=2)
    printer.writers[0].write(b"\x01\x02\xaa\x00garbage")
    printer.status = MachineStatus.PAUSED
    printer.push_all()
    await _wait_for(lambda: client.state.status == MachineStatus.PAUSED)
    await client.disconnect()


async def test_resync_keeps_frame_after_double_start_byte(printer: FakePrinter) -> None:
    client = SnapmakerClient("127.0.0.1", port=printer.port)
    await client.connect(auth_timeout=2)
    # A stray 0xAA directly before a real frame must not swallow its header.
    printer.writers[0].write(b"\xaa")
    printer.status = MachineStatus.PAUSED
    printer.push_all()
    await _wait_for(lambda: client.state.status == MachineStatus.PAUSED)
    await client.disconnect()


async def test_new_job_while_disconnected_is_detected(printer: FakePrinter) -> None:
    client = SnapmakerClient("127.0.0.1", port=printer.port)
    await client.connect(auth_timeout=2)
    printer.status = MachineStatus.PRINTING
    printer.file_name, printer.total_lines, printer.current_line = "a.gcode", 100, 90
    printer.push_all()
    await _wait_for(lambda: client.state.file_name == "a.gcode")
    await client.disconnect()

    # Job A finished and job B started while we were away.
    printer.file_name, printer.total_lines, printer.current_line = "b.gcode", 1000, 100
    await client.connect(auth_timeout=2)
    printer.push_all()
    await _wait_for(lambda: client.state.file_name == "b.gcode")
    await _wait_for(lambda: client.state.progress == 10.0)
    await client.disconnect()


async def test_subscribe_timeout_is_connection_error(printer: FakePrinter, monkeypatch) -> None:
    async def silent(packet, writer):
        if packet.command == (0x01, 0x05):
            printer._reply(writer, packet, b"\x00")
            await writer.drain()

    monkeypatch.setattr(printer, "_respond", silent)
    original = SnapmakerClient._request

    async def short_request(self, receiver, command, payload, timeout=10):
        return await original(self, receiver, command, payload, min(timeout, 0.2))

    monkeypatch.setattr(SnapmakerClient, "_request", short_request)
    client = SnapmakerClient("127.0.0.1", port=printer.port)
    with pytest.raises(SacpConnectionError):
        await client.connect(auth_timeout=2)
    assert not client.connected
