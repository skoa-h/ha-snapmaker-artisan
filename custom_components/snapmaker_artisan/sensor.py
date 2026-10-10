"""Sensors for the Snapmaker Artisan integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import SnapmakerConfigEntry, SnapmakerCoordinator
from .entity import SnapmakerEntity
from .sacp import MachineStatus, PrinterState

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SnapmakerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PrinterState], float | int | str | datetime | None]
    available_fn: Callable[[PrinterState], bool] = lambda _: True


def _nozzle(index: int, attr: str) -> Callable[[PrinterState], float | None]:
    def value(state: PrinterState) -> float | None:
        extruder = state.extruders.get(index)
        return getattr(extruder, attr) if extruder else None

    return value


def _temperature(
    key: str,
    value_fn: Callable[[PrinterState], float | None],
    available_fn: Callable[[PrinterState], bool] = lambda _: True,
) -> SnapmakerSensorDescription:
    return SnapmakerSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        value_fn=value_fn,
        available_fn=available_fn,
    )


SENSORS: tuple[SnapmakerSensorDescription, ...] = (
    SnapmakerSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=[s.name.lower() for s in MachineStatus],
        value_fn=lambda s: s.status.name.lower() if s.status is not None else None,
    ),
    SnapmakerSensorDescription(
        key="progress",
        translation_key="progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda s: s.progress,
    ),
    SnapmakerSensorDescription(
        key="file_name",
        translation_key="file_name",
        value_fn=lambda s: s.file_name,
    ),
    SnapmakerSensorDescription(
        key="elapsed_time",
        translation_key="elapsed_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda s: s.elapsed_time if s.is_active else None,
    ),
    SnapmakerSensorDescription(
        key="remaining_time",
        translation_key="remaining_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda s: s.remaining_time,
    ),
    _temperature("nozzle_temperature", _nozzle(0, "current")),
    _temperature("nozzle_target_temperature", _nozzle(0, "target")),
    _temperature(
        "nozzle_2_temperature",
        _nozzle(1, "current"),
        available_fn=lambda s: 1 in s.extruders,
    ),
    _temperature(
        "nozzle_2_target_temperature",
        _nozzle(1, "target"),
        available_fn=lambda s: 1 in s.extruders,
    ),
    _temperature("bed_temperature", lambda s: s.bed_current),
    _temperature("bed_target_temperature", lambda s: s.bed_target),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SnapmakerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        SnapmakerSensor(coordinator, description) for description in SENSORS
    ]
    entities.append(SnapmakerFinishTimeSensor(coordinator))
    async_add_entities(entities)


class SnapmakerSensor(SnapmakerEntity, SensorEntity):
    entity_description: SnapmakerSensorDescription

    def __init__(
        self, coordinator: SnapmakerCoordinator, description: SnapmakerSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.available_fn(
            self.coordinator.data
        )

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data)


class SnapmakerFinishTimeSensor(SnapmakerEntity, SensorEntity):
    """Estimated wall-clock time the current print finishes."""

    _attr_translation_key = "finish_time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: SnapmakerCoordinator) -> None:
        super().__init__(coordinator, "finish_time")
        self._finish: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        remaining = self.coordinator.data.remaining_time
        if remaining is None or not self.coordinator.data.is_active:
            self._finish = None
            return None
        estimate = (dt_util.utcnow() + timedelta(seconds=remaining)).replace(
            second=0, microsecond=0
        )
        # Don't flap the state by a minute on every update.
        if self._finish is None or abs(estimate - self._finish) > timedelta(minutes=2):
            self._finish = estimate
        return self._finish
