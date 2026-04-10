"""zencontrol scene entities — manually configured DALI scenes."""
from __future__ import annotations

import logging

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_SCENE_ADDRESS,
    CONF_SCENE_NAME,
    CONF_SCENE_NUMBER,
    CONF_SCENES,
    DATA_COORDINATOR,
    DOMAIN,
    UID_SCENE,
    get_entry_config,
)
from .coordinator import ZenControlCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a scene entity for each manually configured scene."""
    coordinator: ZenControlCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    scenes_config: list[dict] = get_entry_config(entry).get(CONF_SCENES, [])

    entities = [
        ZenScene(coordinator, entry, scene_cfg)
        for scene_cfg in scenes_config
    ]
    async_add_entities(entities)


class ZenScene(Scene):
    """A manually configured DALI scene targeting a specific address."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: ZenControlCoordinator,
        entry: ConfigEntry,
        config: dict,
    ) -> None:
        self._coordinator = coordinator
        self._address: int = config[CONF_SCENE_ADDRESS]
        self._scene_number: int = config[CONF_SCENE_NUMBER]

        name = config.get(CONF_SCENE_NAME) or f"Scene {self._scene_number} @ {self._address}"
        self._attr_name = name
        self._attr_unique_id = (
            f"{entry.entry_id}_{UID_SCENE}_{self._address}_{self._scene_number}"
        )
        self._attr_device_info = coordinator.device_info

    async def async_activate(self, **kwargs) -> None:  # type: ignore[override]
        """Recall the scene on the configured address."""
        await self._coordinator.commands.recall_scene(self._address, self._scene_number)
