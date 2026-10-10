"""Base entity for the Snapmaker Artisan integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MODEL, CONF_SERIAL, DOMAIN
from .coordinator import SnapmakerCoordinator


class SnapmakerEntity(CoordinatorEntity[SnapmakerCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SnapmakerCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            manufacturer="Snapmaker",
            model=entry.data.get(CONF_MODEL) or "Snapmaker Artisan",
            name=entry.title,
            serial_number=entry.data.get(CONF_SERIAL),
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.client.connected
