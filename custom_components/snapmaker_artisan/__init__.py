"""The Snapmaker Artisan integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import SnapmakerConfigEntry, SnapmakerCoordinator
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once for the whole integration."""
    async_setup_services(hass)
    return True


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
