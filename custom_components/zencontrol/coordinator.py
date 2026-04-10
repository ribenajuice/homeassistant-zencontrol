"""zencontrol coordinator — per-controller discovery, state management, event routing."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_EVENT_PORT,
    CONF_HOST,
    CONF_PORT,
    CONF_USE_MULTICAST,
    DEFAULT_EVENT_PORT,
    DEFAULT_PORT,
    DOMAIN,
    HARDWARE_MANUFACTURER,
    INTEGRATION_AUTHOR,
    INTEGRATION_AUTHOR_URL,
    PING_INTERVAL,
)
from .tpi import (
    ARC_LEVEL_MIXED,
    ARC_LEVEL_OFF,
    DALI_BROADCAST,
    DALI_GROUP_OFFSET,
    ColourState,
    ColourTempLimits,
    ColourType,
    DeviceColourFeatures,
    EventListener,
    EventType,
    GroupInfo,
    ProfileInfo,
    ProfileInformation,
    TpiClient,
    TpiEvent,
    TpiEventMode,
    ZenCommands,
    DaliCgTypeMask,
    group_to_address,
    is_group_address,
    parse_colour_payload,
)

_LOGGER = logging.getLogger(__name__)

# Coordinator update interval — primarily for the health-check ping.
# Real state updates arrive via push events.
SCAN_INTERVAL = timedelta(seconds=PING_INTERVAL)


# ---------------------------------------------------------------------------
# State containers
# ---------------------------------------------------------------------------

@dataclass
class DeviceState:
    """Cached state for a single DALI address (group or short address)."""
    arc_level: int = ARC_LEVEL_OFF
    colour: ColourState | None = None
    last_scene: int | None = None


@dataclass
class ControllerState:
    """All discovered and live state for one zencontrol controller."""
    label: str = "zencontrol"
    version: tuple[int, int, int] = (0, 0, 0)

    # Groups  {group_number: GroupInfo}
    groups: dict[int, GroupInfo] = field(default_factory=dict)

    # Profiles
    profile_info: ProfileInformation = field(default_factory=ProfileInformation)
    current_profile: int = 0

    # Live light states
    # Keyed by DALI *address* (group address = group_number + 64, or short addr 0-63)
    device_states: dict[int, DeviceState] = field(default_factory=dict)

    # Short address metadata (auto-discovered from the controller)
    short_addresses: list[int] = field(default_factory=list)
    # {address: cg_type_mask}
    short_address_types: dict[int, DaliCgTypeMask] = field(default_factory=dict)
    # {address: DeviceColourFeatures}
    short_address_colour_features: dict[int, DeviceColourFeatures] = field(default_factory=dict)
    # {address: label}
    short_address_labels: dict[int, str] = field(default_factory=dict)
    # {address: ColourTempLimits}
    short_address_ct_limits: dict[int, ColourTempLimits] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class ZenControlCoordinator(DataUpdateCoordinator[ControllerState]):
    """Manages one zencontrol Application Controller.

    Responsibilities:
    - Connects to the controller via UDP/TCP.
    - Discovers groups, scenes, and profiles on startup.
    - Registers itself with the shared EventListener and enables TPI events.
    - Handles push events from the controller to update entity state.
    - Periodically pings the controller; re-asserts event config if needed.
    - Exposes ZenCommands for entity service calls.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, entry_data: dict[str, Any]) -> None:
        self._entry_id = entry_id
        self._host: str = entry_data[CONF_HOST]
        self._port: int = entry_data.get(CONF_PORT, DEFAULT_PORT)
        self._event_port: int = entry_data.get(CONF_EVENT_PORT, DEFAULT_EVENT_PORT)
        self._use_multicast: bool = entry_data.get(CONF_USE_MULTICAST, False)

        self._client = TpiClient(host=self._host, port=self._port)
        self.commands = ZenCommands(self._client)

        super().__init__(
            hass,
            _LOGGER,
            name=f"zencontrol {self._host}",
            update_interval=SCAN_INTERVAL,
        )
        # data is initialised by DataUpdateCoordinator to None until first fetch
        self.data = ControllerState()

    # ------------------------------------------------------------------
    # DataUpdateCoordinator overrides
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> ControllerState:
        """Called on startup and every SCAN_INTERVAL seconds.

        On first call: full discovery.
        On subsequent calls: health-check ping + re-assert events if needed.
        """
        if not self._client.connected:
            try:
                await self._client.connect()
            except OSError as exc:
                raise UpdateFailed(f"Cannot connect to {self._host}: {exc}") from exc

        # Health check — re-assert event configuration if controller rebooted
        await self._check_and_assert_events()

        # Full discovery only on the very first update
        if not self.data.label or self.data.label == "zencontrol":
            await self._discover()

        return self.data

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def _discover(self) -> None:
        """Query controller metadata and auto-discover groups/scenes/profiles."""
        _LOGGER.debug("Starting discovery for %s", self._host)

        # Wait for controller startup — up to 60 s in 5 s increments
        for attempt in range(12):
            if await self.commands.query_startup_complete():
                break
            _LOGGER.debug("Controller %s not ready yet (attempt %d/12)", self._host, attempt + 1)
            await asyncio.sleep(5)
        else:
            _LOGGER.warning(
                "Controller %s startup did not complete after 60 s — continuing anyway",
                self._host,
            )

        # Basic controller info
        label = await self.commands.query_controller_label()
        self.data.label = label or self._host

        version = await self.commands.query_controller_version()
        if version:
            self.data.version = version
            _LOGGER.info(
                "Connected to '%s' firmware v%d.%d.%d",
                self.data.label,
                *version,
            )

        # Groups
        groups = await self.commands.query_groups()
        for group in groups:
            self.data.groups[group.number] = group
            # Initialise state entry for this group address
            addr = group_to_address(group.number)
            if addr not in self.data.device_states:
                self.data.device_states[addr] = DeviceState()

        # Profiles — query info (list + current from PROFILE_INFORMATION),
        # then confirm current with the dedicated QUERY_CURRENT_PROFILE_NUMBER
        # which is reliable even when the controller is running on schedule.
        self.data.profile_info = await self.commands.query_profile_information()
        self.data.current_profile = self.data.profile_info.current_profile
        current = await self.commands.query_current_profile_number()
        if current is not None:
            self.data.current_profile = current

        # Short addresses (metadata only — entities are created by the user)
        await self._discover_short_addresses()

        # Initial state poll for all known addresses
        await self._poll_all_states()

        _LOGGER.debug(
            "Discovery complete for %s: %d groups, %d short addresses, %d profiles",
            self._host,
            len(self.data.groups),
            len(self.data.short_addresses),
            len(self.data.profile_info.profiles),
        )

    async def _discover_short_addresses(self) -> None:
        """Auto-discover all DALI short addresses and query their metadata."""
        addresses = await self.commands.query_control_gear_addresses()
        self.data.short_addresses = addresses
        _LOGGER.debug("Discovered %d short addresses on %s: %s", len(addresses), self._host, addresses)

        for addr in addresses:
            cg_type = await self.commands.query_cg_type(addr)
            self.data.short_address_types[addr] = cg_type

            features = await self.commands.query_colour_features(addr)
            self.data.short_address_colour_features[addr] = features

            label = await self.commands.query_device_label(addr) or f"Light {addr}"
            self.data.short_address_labels[addr] = label

            if features.tc:
                limits = await self.commands.query_colour_temp_limits(addr)
                if limits:
                    self.data.short_address_ct_limits[addr] = limits

            if addr not in self.data.device_states:
                self.data.device_states[addr] = DeviceState()

    async def _poll_all_states(self) -> None:
        """Poll the current arc level (and colour) for all known addresses."""
        addresses_to_poll: list[int] = []

        # Group addresses (64-79)
        for gnum in self.data.groups:
            addresses_to_poll.append(group_to_address(gnum))

        # Short addresses
        addresses_to_poll.extend(self.data.short_addresses)

        for addr in addresses_to_poll:
            level = await self.commands.query_level(addr)
            if level is not None:
                state = self.data.device_states.setdefault(addr, DeviceState())
                state.arc_level = level

            # Poll colour for colour-capable short addresses
            if addr < DALI_GROUP_OFFSET:
                features = self.data.short_address_colour_features.get(addr)
                if features and features.supports_colour:
                    colour = await self.commands.query_colour(addr)
                    if colour:
                        self.data.device_states[addr].colour = colour
            else:
                # Poll colour for group addresses too — groups may contain colour fixtures.
                # QUERY_DALI_COLOUR returns NO_ANSWER for non-colour groups; that's fine.
                colour = await self.commands.query_colour(addr)
                if colour:
                    self.data.device_states[addr].colour = colour

    # ------------------------------------------------------------------
    # TPI event configuration
    # ------------------------------------------------------------------

    async def setup_events(self, listener: EventListener) -> None:
        """Register with the shared listener and configure the controller."""
        self._listener = listener
        listener.register(self._host, self._on_event)

        if not self._use_multicast:
            ha_ip = await self._get_ha_ip()
            if ha_ip:
                ok = await self.commands.configure_unicast_events(ha_ip, self._event_port)
                if ok:
                    _LOGGER.debug(
                        "Unicast events configured: %s → %s:%d",
                        self._host,
                        ha_ip,
                        self._event_port,
                    )
                else:
                    _LOGGER.warning(
                        "Failed to configure unicast events for %s", self._host
                    )
            else:
                _LOGGER.warning("Could not determine HA IP for unicast events")
        else:
            # Multicast — just enable events on the controller
            await self.commands.enable_events_unicast(TpiEventMode.ENABLED)

    async def _check_and_assert_events(self) -> None:
        """Ping controller; re-assert event config if it has rebooted."""
        state = await self.commands.query_event_emit_state()
        if state is None:
            _LOGGER.debug("No response from %s during ping", self._host)
            return
        expected_bit = int(TpiEventMode.ENABLED)
        if not (state & expected_bit):
            _LOGGER.info(
                "Controller %s events not enabled (state=0x%02X) — re-asserting",
                self._host,
                state,
            )
            ha_ip = await self._get_ha_ip()
            if ha_ip and not self._use_multicast:
                await self.commands.configure_unicast_events(ha_ip, self._event_port)
            else:
                await self.commands.enable_events_unicast(TpiEventMode.ENABLED)

    async def _get_ha_ip(self) -> str | None:
        """Resolve HA's outbound IP toward the controller."""
        try:
            # Try the HA network helper first
            from homeassistant.components.network import async_get_source_ip
            ip = await async_get_source_ip(self.hass, target_ip=self._host)
            if ip:
                return ip
        except Exception:
            pass

        # Fallback: open a UDP socket and read the local address
        try:
            sock = await asyncio.get_event_loop().run_in_executor(
                None, self._resolve_local_ip
            )
            return sock
        except Exception:
            return None

    def _resolve_local_ip(self) -> str | None:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((self._host, self._port))
                return s.getsockname()[0]
        except OSError:
            return None

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    @callback
    def _on_event(self, source_ip: str, event: TpiEvent) -> None:
        """Dispatch an incoming TPI event to the appropriate state update."""
        try:
            if event.event_type == EventType.LEVEL_CHANGE_V2:
                self._handle_level_change_v2(event)
            elif event.event_type == EventType.LEVEL_CHANGE:
                self._handle_level_change(event)
            elif event.event_type == EventType.GROUP_LEVEL_CHANGE:
                self._handle_group_level_change(event)
            elif event.event_type == EventType.COLOUR_CHANGED:
                self._handle_colour_changed(event)
            elif event.event_type == EventType.SCENE_CHANGE:
                self._handle_scene_change(event)
            elif event.event_type == EventType.PROFILE_CHANGED:
                self._handle_profile_changed(event)
        except Exception:
            _LOGGER.exception("Error handling TPI event type 0x%02X", event.event_type)

    def _handle_level_change_v2(self, event: TpiEvent) -> None:
        """LEVEL_CHANGE_EVENT_V2: target = address/group, data = [arc_level, dimming_to]."""
        addr = event.target
        if not event.data:
            return
        arc_level = event.data[0]
        state = self.data.device_states.setdefault(addr, DeviceState())
        state.arc_level = arc_level
        self.async_set_updated_data(self.data)

    def _handle_level_change(self, event: TpiEvent) -> None:
        """LEVEL_CHANGE_EVENT: target = address, data = [arc_level]."""
        addr = event.target
        if not event.data:
            return
        arc_level = event.data[0]
        state = self.data.device_states.setdefault(addr, DeviceState())
        # Only update if we don't have a V2 listener active (avoid double updates)
        state.arc_level = arc_level
        self.async_set_updated_data(self.data)

    def _handle_group_level_change(self, event: TpiEvent) -> None:
        """GROUP_LEVEL_CHANGE_EVENT: target = group number, data = [arc_level]."""
        group_num = event.target
        addr = group_to_address(group_num)
        if not event.data:
            return
        arc_level = event.data[0]
        state = self.data.device_states.setdefault(addr, DeviceState())
        state.arc_level = arc_level
        self.async_set_updated_data(self.data)

    def _handle_colour_changed(self, event: TpiEvent) -> None:
        """COLOUR_CHANGED_EVENT: target = address or group, data = [colour_type, ...]."""
        if len(event.data) < 1:
            return
        try:
            colour_type = ColourType(event.data[0])
        except ValueError:
            return
        state = self.data.device_states.setdefault(event.target, DeviceState())
        state.colour = parse_colour_payload(colour_type, event.data[1:])
        self.async_set_updated_data(self.data)

    def _handle_scene_change(self, event: TpiEvent) -> None:
        """SCENE_CHANGE_EVENT: target = address, data = [last_scene, at_scene]."""
        addr = event.target
        if not event.data:
            return
        scene_num = event.data[0]
        state = self.data.device_states.setdefault(addr, DeviceState())
        state.last_scene = scene_num
        self.async_set_updated_data(self.data)

    def _handle_profile_changed(self, event: TpiEvent) -> None:
        """PROFILE_CHANGED_EVENT: data = [profile_hi, profile_lo]."""
        if len(event.data) < 2:
            return
        profile_id = (event.data[0] << 8) | event.data[1]
        self.data.current_profile = profile_id
        self.async_set_updated_data(self.data)

    # ------------------------------------------------------------------
    # Helpers for entities
    # ------------------------------------------------------------------

    def get_device_state(self, address: int) -> DeviceState:
        return self.data.device_states.get(address, DeviceState())

    @property
    def device_info(self) -> DeviceInfo:
        """DeviceInfo shared by all entities belonging to this controller."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self.data.label,
            manufacturer=HARDWARE_MANUFACTURER,
            sw_version="{}.{}.{}".format(*self.data.version),
            configuration_url=INTEGRATION_AUTHOR_URL,
            via_device=None,
        )

    async def async_disconnect(self) -> None:
        """Disconnect from the controller."""
        await self._client.disconnect()

