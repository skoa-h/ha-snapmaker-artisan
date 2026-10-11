"""Tests for the Home Assistant side of the integration."""

from __future__ import annotations

import asyncio
from functools import partial
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.snapmaker_artisan.const import DOMAIN
from custom_components.snapmaker_artisan.sacp import (
    DiscoveredPrinter,
    MachineStatus,
    SnapmakerClient,
)

from .fake_printer import FakePrinter

pytestmark = pytest.mark.usefixtures("socket_enabled")


@pytest.fixture
async def printer():
    fake = FakePrinter()
    await fake.start()
    client_factory = partial(SnapmakerClient, port=fake.port)
    with (
        patch("custom_components.snapmaker_artisan.config_flow.SnapmakerClient", client_factory),
        patch("custom_components.snapmaker_artisan.coordinator.SnapmakerClient", client_factory),
        patch(
            "custom_components.snapmaker_artisan.config_flow.discover",
            return_value=[DiscoveredPrinter("127.0.0.1", "Snapmaker A400XYZ", "Snapmaker Artisan")],
        ),
        patch("custom_components.snapmaker_artisan.coordinator.UPDATE_DEBOUNCE", 0.01),
        patch("custom_components.snapmaker_artisan.coordinator.RECONNECT_DELAY", 0.05),
    ):
        yield fake
    await fake.stop()


async def _wait(hass: HomeAssistant, predicate, timeout=3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()


async def _setup_via_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    # The discovered printer is pre-filled.
    assert "Snapmaker Artisan (127.0.0.1)" in result["description_placeholders"]["found"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == "Snapmaker Artisan"
    await hass.async_block_till_done()
    return result["result"].entry_id


async def test_full_flow_and_sensors(hass: HomeAssistant, printer: FakePrinter) -> None:
    entry_id = await _setup_via_flow(hass)
    # The flow's connection is reused: only one connect request was sent.
    assert sum(p.command == (0x01, 0x05) for p in printer.received) == 1

    printer.status = MachineStatus.PRINTING
    printer.file_name = "benchy.gcode"
    printer.total_lines = 1000
    printer.current_line = 420
    printer.estimate = 3600
    printer.elapsed = 1200
    printer.push_all()
    await _wait(hass, lambda: hass.states.get("sensor.snapmaker_artisan_print_file").state == "benchy.gcode")
    printer.push_all()
    await _wait(hass, lambda: hass.states.get("sensor.snapmaker_artisan_print_progress").state == "42.0")

    states = {s.entity_id: s.state for s in hass.states.async_all("sensor")}
    assert states["sensor.snapmaker_artisan_status"] == "printing"
    assert states["sensor.snapmaker_artisan_current_line"] == "420"
    assert states["sensor.snapmaker_artisan_nozzle_temperature"] == "210.5"
    assert states["sensor.snapmaker_artisan_nozzle_2_temperature"] == "25.0"
    assert states["sensor.snapmaker_artisan_bed_temperature"] == "59.8"
    assert states["sensor.snapmaker_artisan_bed_target_temperature"] == "60.0"
    assert float(states["sensor.snapmaker_artisan_print_time_elapsed"]) == 20  # minutes
    # 0.58² * 3600 + 0.58 * 1200 = 1907 s
    assert round(float(states["sensor.snapmaker_artisan_print_time_remaining"])) == 32
    assert states["sensor.snapmaker_artisan_print_finish_time"] not in ("unknown", STATE_UNAVAILABLE)

    printer.status = MachineStatus.COMPLETED
    printer.push_all()
    await _wait(hass, lambda: hass.states.get("sensor.snapmaker_artisan_print_progress").state == "100.0")
    assert hass.states.get("sensor.snapmaker_artisan_print_finish_time").state == "unknown"

    # Touchscreen drops us -> unavailable -> reconnect -> available again.
    printer.request_disconnect()
    await _wait(hass, lambda: hass.states.get("sensor.snapmaker_artisan_status").state == STATE_UNAVAILABLE)
    await _wait(hass, lambda: sum(p.command == (0x01, 0x05) for p in printer.received) == 2)
    printer.status = MachineStatus.IDLE
    await _wait(hass, lambda: len(printer.writers) == 2 and printer.subscriptions)
    await asyncio.sleep(0.1)
    printer.push_all()
    await _wait(hass, lambda: hass.states.get("sensor.snapmaker_artisan_status").state == "idle")

    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()


async def test_flow_not_confirmed(hass: HomeAssistant, printer: FakePrinter) -> None:
    printer.accept_connection = False
    with patch("custom_components.snapmaker_artisan.config_flow.AUTH_TIMEOUT", 0.2):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_confirmed"}


async def test_flow_cannot_connect(hass: HomeAssistant, printer: FakePrinter) -> None:
    await printer.stop()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_unload_with_pending_debounced_update(hass: HomeAssistant, printer: FakePrinter) -> None:
    with patch("custom_components.snapmaker_artisan.coordinator.UPDATE_DEBOUNCE", 30):
        entry_id = await _setup_via_flow(hass)
        printer.push_all()
        coordinator = hass.config_entries.async_get_entry(entry_id).runtime_data
        await _wait(hass, lambda: coordinator._pending_update is not None)
        assert await hass.config_entries.async_unload(entry_id)
        await hass.async_block_till_done()
        assert coordinator._pending_update is None
        assert not coordinator.client.connected


async def test_connection_switch(hass: HomeAssistant, printer: FakePrinter) -> None:
    entry_id = await _setup_via_flow(hass)
    switch = "switch.snapmaker_artisan_connection"
    status = "sensor.snapmaker_artisan_status"
    connects = lambda: sum(p.command == (0x01, 0x05) for p in printer.received)  # noqa: E731
    printer.push_all()
    await _wait(hass, lambda: hass.states.get(status).state == "idle")
    assert hass.states.get(switch).state == "on"

    # Off: polite disconnect, sensors unavailable, switch stays usable.
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    await _wait(hass, lambda: any(p.command == (0x01, 0x06) for p in printer.received))
    assert hass.states.get(switch).state == "off"
    assert hass.states.get(status).state == STATE_UNAVAILABLE
    entry = hass.config_entries.async_get_entry(entry_id)
    assert entry.options["connection_enabled"] is False
    # ...and it doesn't come back on its own (RECONNECT_DELAY is 0.05 s here).
    await asyncio.sleep(0.3)
    assert connects() == 1

    # Still off after a reload / Home Assistant restart.
    assert await hass.config_entries.async_reload(entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0.3)
    assert connects() == 1
    assert hass.states.get(switch).state == "off"

    # On: reconnects.
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await _wait(hass, lambda: connects() == 2 and printer.subscriptions)
    await _wait(hass, lambda: hass.config_entries.async_get_entry(entry_id).runtime_data.client.connected)
    printer.push_all()
    await _wait(hass, lambda: hass.states.get(status).state == "idle")
    assert hass.states.get(switch).state == "on"

    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()
