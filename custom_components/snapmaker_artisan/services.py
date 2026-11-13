"""Services of the Snapmaker Artisan integration."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import SnapmakerConfigEntry
from .sacp import SacpError

_LOGGER = logging.getLogger(__name__)

SERVICE_UPLOAD_GCODE = "upload_gcode"
ATTR_DEVICE_ID = "device_id"
ATTR_FILE_PATH = "file_path"
ATTR_FILENAME = "filename"

UPLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_FILE_PATH): cv.string,
        vol.Optional(ATTR_FILENAME): cv.string,
    }
)


def _read_file(path: str) -> bytes:
    return Path(path).read_bytes()


def _get_entry(hass: HomeAssistant, device_id: str) -> SnapmakerConfigEntry:
    device = dr.async_get(hass).async_get(device_id)
    if device is not None:
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry and entry.domain == DOMAIN:
                return entry
    raise ServiceValidationError(f"{device_id} is not a Snapmaker Artisan device")


async def async_upload_to_entry(entry: SnapmakerConfigEntry, name: str, data: bytes) -> None:
    """Upload G-code to the printer of a config entry (shared by service and web UI)."""
    if not hasattr(entry, "runtime_data"):
        raise ServiceValidationError("The Snapmaker Artisan entry is not loaded")
    client = entry.runtime_data.client
    if not client.connected:
        raise HomeAssistantError(
            "Not connected to the printer (is the Connection switch on?)"
        )
    if client.state.is_active:
        raise HomeAssistantError("The printer is busy with a print job")
    _LOGGER.debug("Uploading %s (%d bytes) to %s", name, len(data), client.host)
    try:
        await client.upload_file(name, data)
    except SacpError as err:
        raise HomeAssistantError(f"Upload failed: {err}") from err


async def _async_upload_gcode(call: ServiceCall) -> None:
    hass = call.hass
    entry = _get_entry(hass, call.data[ATTR_DEVICE_ID])
    path: str = call.data[ATTR_FILE_PATH]
    if not hass.config.is_allowed_path(path):
        raise ServiceValidationError(
            f"{path} is not allowed, add its folder to allowlist_external_dirs"
        )
    if not os.path.isfile(path):
        raise ServiceValidationError(f"{path} does not exist")
    name = call.data.get(ATTR_FILENAME) or os.path.basename(path)
    data = await hass.async_add_executor_job(_read_file, path)
    await async_upload_to_entry(entry, name, data)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's services."""
    hass.services.async_register(
        DOMAIN, SERVICE_UPLOAD_GCODE, _async_upload_gcode, schema=UPLOAD_SCHEMA
    )
