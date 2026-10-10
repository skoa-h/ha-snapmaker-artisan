"""Connection switch for the Snapmaker Artisan integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SnapmakerConfigEntry, SnapmakerCoordinator
from .entity import SnapmakerEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SnapmakerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([SnapmakerConnectionSwitch(entry.runtime_data)])


class SnapmakerConnectionSwitch(SnapmakerEntity, SwitchEntity):
    """Keep Home Assistant connected to the printer, or let it go.

    The printer only talks to one SACP client at a time, so this is how
    the user frees it for Snapmaker Luban. The state is the user's choice,
    not whether the printer is currently reachable.
    """

    _attr_translation_key = "connection"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SnapmakerCoordinator) -> None:
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        # Must stay usable while disconnected, or it couldn't reconnect.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.connection_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_connection_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_connection_enabled(False)
