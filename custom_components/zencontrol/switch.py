"""zencontrol switch entities — DALI relay devices at manually-added short addresses."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DOMAIN, UID_SHORT
from .coordinator import DeviceState, ZenControlCoordinator
from .tpi import ARC_LEVEL_MAX, ARC_LEVEL_OFF, DaliCgTypeMask

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create switch entities for relay-type short addresses."""
    coordinator: ZenControlCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    entities: list[ZenRelaySwitch] = []

    for addr in coordinator.data.short_addresses:
        cg_type = coordinator.data.short_address_types.get(addr, DaliCgTypeMask(0))
        if DaliCgTypeMask.RELAY in cg_type:
            entities.append(ZenRelaySwitch(coordinator, entry, addr))

    async_add_entities(entities)


class ZenRelaySwitch(CoordinatorEntity[ZenControlCoordinator], SwitchEntity):
    """Switch entity for a DALI relay at a fixed short address."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: ZenControlCoordinator,
        entry: ConfigEntry,
        address: int,
    ) -> None:
        super().__init__(coordinator)
        self._address = address
        label = coordinator.data.short_address_labels.get(address, f"Relay {address}")
        self._attr_name = label
        self._attr_unique_id = f"{entry.entry_id}_{UID_SHORT}_relay_{address}"
        self._attr_device_info = coordinator.device_info
        self._attr_extra_state_attributes = {"dali_address": address}

    @property
    def _device_state(self) -> DeviceState:
        return self.coordinator.get_device_state(self._address)

    @property
    def is_on(self) -> bool:
        return self._device_state.arc_level != ARC_LEVEL_OFF

    async def async_turn_on(self, **kwargs) -> None:  # type: ignore[override]
        await self.coordinator.commands.recall_max(self._address)
        # Optimistic update — don't wait for push event
        state = self.coordinator.data.device_states.get(self._address)
        if state is not None:
            state.arc_level = ARC_LEVEL_MAX
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:  # type: ignore[override]
        await self.coordinator.commands.set_off(self._address)
        # Optimistic update — don't wait for push event
        state = self.coordinator.data.device_states.get(self._address)
        if state is not None:
            state.arc_level = ARC_LEVEL_OFF
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
