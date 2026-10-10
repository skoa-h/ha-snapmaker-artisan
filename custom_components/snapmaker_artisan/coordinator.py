"""Connection management for the Snapmaker Artisan integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    AUTH_TIMEOUT,
    CLIENT_NAME,
    DOMAIN,
    RECONNECT_DELAY,
    RECONNECT_DELAY_AUTH,
)
from .sacp import PrinterState, SacpAuthError, SacpConnectionError, SnapmakerClient

_LOGGER = logging.getLogger(__name__)

type SnapmakerConfigEntry = ConfigEntry[SnapmakerCoordinator]

# The printer pushes several packets per second; batch them into one update.
UPDATE_DEBOUNCE = 1.0


class SnapmakerCoordinator(DataUpdateCoordinator[PrinterState]):
    """Keeps a push connection to the printer alive and fans out updates."""

    config_entry: SnapmakerConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SnapmakerConfigEntry,
        client: SnapmakerClient | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_HOST]}",
            update_interval=None,
        )
        self._connection_lost = asyncio.Event()
        self._pending_update: asyncio.TimerHandle | None = None
        if client is None:
            client = SnapmakerClient(entry.data[CONF_HOST], client_name=CLIENT_NAME)
        # Reuse the already-authorised connection from the config flow if any.
        self.client = client
        self.client.set_callbacks(self._handle_update, self._handle_disconnect)
        self.data = client.state
        self.last_update_success = client.connected

    async def _async_update_data(self) -> PrinterState:
        return self.client.state

    @callback
    def _handle_update(self, state: PrinterState) -> None:
        if self._pending_update is None:
            self._pending_update = self.hass.loop.call_later(
                UPDATE_DEBOUNCE, self._flush_update
            )

    @callback
    def _flush_update(self) -> None:
        self._pending_update = None
        self.async_set_updated_data(self.client.state)

    @callback
    def _handle_disconnect(self) -> None:
        self._connection_lost.set()

    async def async_run(self) -> None:
        """Connect, wait for the connection to drop, reconnect. Forever."""
        while True:
            if not self.client.connected:
                try:
                    await self.client.connect(auth_timeout=AUTH_TIMEOUT)
                except SacpAuthError as err:
                    _LOGGER.warning(
                        "Snapmaker at %s did not accept the connection (%s). Confirm "
                        "the request on the touchscreen; retrying in %s s",
                        self.client.host, err, RECONNECT_DELAY_AUTH,
                    )
                    await asyncio.sleep(RECONNECT_DELAY_AUTH)
                    continue
                except SacpConnectionError as err:
                    _LOGGER.debug("Snapmaker unavailable: %s", err)
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue
                except Exception:
                    _LOGGER.exception("Unexpected error connecting to %s", self.client.host)
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue
                _LOGGER.info("Connected to Snapmaker at %s", self.client.host)

            self._connection_lost.clear()
            self.async_set_updated_data(self.client.state)
            await self._connection_lost.wait()

            _LOGGER.info("Lost connection to Snapmaker at %s", self.client.host)
            await self.client.disconnect()
            self.last_update_success = False
            self.async_update_listeners()
            await asyncio.sleep(RECONNECT_DELAY)

    async def async_shutdown(self) -> None:
        if self._pending_update:
            self._pending_update.cancel()
            self._pending_update = None
        await self.client.disconnect()
        await super().async_shutdown()
