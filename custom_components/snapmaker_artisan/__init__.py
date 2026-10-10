"""The Snapmaker Artisan integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SnapmakerConfigEntry, SnapmakerCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: SnapmakerConfigEntry) -> bool:
    """Set up Snapmaker Artisan from a config entry."""
    # The config flow hands over its authorised connection so the user
    # doesn't get asked to confirm on the touchscreen twice.
    client = None
    if handover := hass.data.get(DOMAIN, {}).pop(entry.data[CONF_HOST], None):
        client, drop_timer = handover
        drop_timer.cancel()
    coordinator = SnapmakerCoordinator(hass, entry, client)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SnapmakerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
