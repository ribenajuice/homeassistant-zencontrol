"""zencontrol occupancy binary sensor entities."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DOMAIN, UID_OCCUPANCY
from .coordinator import OccupancySensorInfo, ZenControlCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create occupancy binary sensor entities for all discovered instances."""
    coordinator: ZenControlCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]

    entities = [
        ZenOccupancySensor(coordinator, entry, sensor)
        for sensor in coordinator.data.occupancy_sensors
    ]

    if entities:
        async_add_entities(entities)
    else:
        _LOGGER.debug(
            "No occupancy sensors found for %s — skipping binary_sensor entities",
            coordinator.data.label,
        )


class ZenOccupancySensor(CoordinatorEntity[ZenControlCoordinator], BinarySensorEntity):
    """Binary sensor representing one DALI occupancy sensor instance."""

    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(
        self,
        coordinator: ZenControlCoordinator,
        entry: ConfigEntry,
        sensor: OccupancySensorInfo,
    ) -> None:
        super().__init__(coordinator)
        self._cd_address = sensor.cd_address
        self._instance_number = sensor.instance_number
        self._attr_unique_id = (
            f"{entry.entry_id}_{UID_OCCUPANCY}_{sensor.cd_address}_{sensor.instance_number}"
        )
        self._attr_name = sensor.label
        self._attr_device_info = coordinator.device_info
        self._attr_extra_state_attributes = {
            "cd_address": sensor.cd_address,
            "instance": sensor.instance_number,
            "hold_time_s": sensor.hold_time_s,
        }

    @property
    def is_on(self) -> bool:
        """Return True when the sensor reports occupancy."""
        return self.coordinator.data.sensor_occupancy.get(
            (self._cd_address, self._instance_number), False
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
