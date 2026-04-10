"""High-level TPI Advanced command wrappers.

Each method builds the appropriate frame, sends it via the TpiClient, and
returns a parsed result (or None / raises on failure).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .client import TpiClient
from .const import (
    ARC_LEVEL_OFF,
    DALI_BROADCAST,
    Command,
    ColourType,
    DaliCgTypeMask,
    DaliStatusMask,
    ResponseType,
    TpiEventMode,
    parse_colour_features,
)
from .protocol import (
    Response,
    build_basic_frame,
    build_dali_colour_frame,
    build_dynamic_frame,
    build_rgbwaf_colour_data,
    build_tc_colour_data,
    build_unicast_address_frame,
    build_xy_colour_data,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures returned by query commands
# ---------------------------------------------------------------------------

@dataclass
class GroupInfo:
    number: int
    label: str = ""


@dataclass
class SceneInfo:
    number: int
    label: str = ""


@dataclass
class ProfileInfo:
    number: int
    label: str = ""
    behaviour: int = 0


@dataclass
class ColourTempLimits:
    physical_warmest_k: int = 2700
    physical_coolest_k: int = 6500
    soft_warmest_k: int = 2700
    soft_coolest_k: int = 6500
    step_k: int = 100


@dataclass
class ColourState:
    colour_type: ColourType | None = None
    kelvin: int | None = None
    r: int | None = None
    g: int | None = None
    b: int | None = None
    w: int | None = None
    a: int | None = None
    f: int | None = None
    x: int | None = None
    y: int | None = None


def parse_colour_payload(colour_type: ColourType, payload: bytes) -> ColourState:
    """Parse colour channel bytes (everything after the colour-type byte) into a ColourState.

    Used by both query_colour (response frames) and _handle_colour_changed (event frames).
    RGBWAF fixtures send only as many channel bytes as they have channels (RGB=3, RGBW=4, …).
    """
    state = ColourState(colour_type=colour_type)
    if colour_type == ColourType.TC and len(payload) >= 2:
        state.kelvin = (payload[0] << 8) | payload[1]
    elif colour_type == ColourType.RGBWAF and len(payload) >= 3:
        state.r = payload[0] if len(payload) > 0 else None
        state.g = payload[1] if len(payload) > 1 else None
        state.b = payload[2] if len(payload) > 2 else None
        state.w = payload[3] if len(payload) > 3 else None
        state.a = payload[4] if len(payload) > 4 else None
        state.f = payload[5] if len(payload) > 5 else None
    elif colour_type == ColourType.XY and len(payload) >= 4:
        state.x = (payload[0] << 8) | payload[1]
        state.y = (payload[2] << 8) | payload[3]
    return state


@dataclass
class DeviceColourFeatures:
    xy: bool = False
    tc: bool = False
    primaries: int = 0
    rgbwaf_channels: int = 0

    @property
    def supports_colour(self) -> bool:
        return self.xy or self.tc or self.rgbwaf_channels > 0


@dataclass
class ControllerInfo:
    label: str = ""
    version: tuple[int, int, int] = (0, 0, 0)
    startup_complete: bool = False
    dali_ready: bool = False


@dataclass
class ProfileInformation:
    current_profile: int = 0
    last_scheduled_profile: int = 0
    profiles: list[ProfileInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Command class
# ---------------------------------------------------------------------------

class ZenCommands:
    """Wraps TpiClient to provide typed command methods."""

    def __init__(self, client: TpiClient) -> None:
        self._c = client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send(
        self,
        cmd: Command,
        address: int = 0,
        data_hi: int = 0,
        data_mid: int = 0,
        data_lo: int = 0,
    ) -> Response | None:
        seq = self._c.next_seq()
        frame = build_basic_frame(seq, cmd, address, data_hi, data_mid, data_lo)
        try:
            return await self._c.send(frame, seq)
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout on command 0x%02X to address %d", cmd, address)
            return None
        except ConnectionError as exc:
            _LOGGER.debug("Connection error on command 0x%02X: %s", cmd, exc)
            return None

    async def _send_dynamic(self, cmd: Command, data: bytes) -> Response | None:
        seq = self._c.next_seq()
        frame = build_dynamic_frame(seq, cmd, data)
        try:
            return await self._c.send(frame, seq)
        except (asyncio.TimeoutError, ConnectionError) as exc:
            _LOGGER.debug("Error on dynamic command 0x%02X: %s", cmd, exc)
            return None

    # ------------------------------------------------------------------
    # Controller info
    # ------------------------------------------------------------------

    async def query_controller_label(self) -> str | None:
        resp = await self._send(Command.QUERY_CONTROLLER_LABEL)
        if resp and resp.has_data:
            return resp.data.decode("utf-8", errors="replace")
        return None

    async def query_controller_version(self) -> tuple[int, int, int] | None:
        resp = await self._send(Command.QUERY_CONTROLLER_VERSION_NUMBER)
        if resp and resp.has_data and len(resp.data) >= 3:
            return (resp.data[0], resp.data[1], resp.data[2])
        return None

    async def query_startup_complete(self) -> bool:
        resp = await self._send(Command.QUERY_CONTROLLER_STARTUP_COMPLETE)
        if resp and resp.has_data:
            return bool(resp.data[0])
        return False

    async def query_dali_ready(self) -> bool:
        resp = await self._send(Command.QUERY_IS_DALI_READY)
        if resp and resp.has_data:
            return bool(resp.data[0])
        return False

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    async def query_group_numbers(self) -> list[int]:
        resp = await self._send(Command.QUERY_GROUP_NUMBERS)
        if resp and resp.has_data:
            return list(resp.data)
        return []

    async def query_group_label(self, group: int) -> str | None:
        """group: 0-15"""
        resp = await self._send(Command.QUERY_GROUP_LABEL, address=group)
        if resp and resp.has_data:
            return resp.data.decode("utf-8", errors="replace")
        return None

    async def query_groups(self) -> list[GroupInfo]:
        """Return a list of GroupInfo for all groups on the controller."""
        numbers = await self.query_group_numbers()
        labels = await asyncio.gather(*[self.query_group_label(n) for n in numbers])
        return [
            GroupInfo(number=n, label=lbl or f"Group {n}")
            for n, lbl in zip(numbers, labels)
        ]

    # ------------------------------------------------------------------
    # Scenes
    # ------------------------------------------------------------------

    async def query_scene_numbers_for_group(self, group: int) -> list[int]:
        """group: 0-15"""
        resp = await self._send(Command.QUERY_SCENE_NUMBERS_FOR_GROUP, address=group)
        if resp and resp.has_data:
            return list(resp.data)
        return []

    async def query_scene_label_for_group(self, group: int, scene: int) -> str | None:
        """group: 0-15, scene: 0-12"""
        resp = await self._send(
            Command.QUERY_SCENE_LABEL_FOR_GROUP,
            address=group,
            data_lo=scene,
        )
        if resp and resp.has_data:
            return resp.data.decode("utf-8", errors="replace")
        return None

    async def query_scenes_for_group(self, group: int) -> list[SceneInfo]:
        """Return all SceneInfo objects for a group."""
        numbers = await self.query_scene_numbers_for_group(group)
        scenes = []
        for num in numbers:
            label = await self.query_scene_label_for_group(group, num) or f"Scene {num}"
            scenes.append(SceneInfo(number=num, label=label))
        return scenes

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    async def query_profile_information(self) -> ProfileInformation:
        """Return current profile state and the list of available profiles."""
        resp = await self._send(Command.QUERY_PROFILE_INFORMATION)
        result = ProfileInformation()
        if not (resp and resp.has_data and len(resp.data) >= 4):
            return result

        d = resp.data
        result.current_profile = (d[0] << 8) | d[1]
        result.last_scheduled_profile = (d[2] << 8) | d[3]
        # Skip 8 bytes of UTC timestamps (bytes 4-11)
        offset = 12
        while offset + 2 <= len(d):
            profile_num = (d[offset] << 8) | d[offset + 1]
            behaviour = d[offset + 2] if offset + 2 < len(d) else 0
            result.profiles.append(ProfileInfo(number=profile_num, behaviour=behaviour))
            offset += 3

        labels = await asyncio.gather(*[self.query_profile_label(p.number) for p in result.profiles])
        for p, lbl in zip(result.profiles, labels):
            p.label = lbl or f"Profile {p.number}"

        return result

    async def query_profile_label(self, profile_id: int) -> str | None:
        """profile_id is a 2-byte value."""
        hi = (profile_id >> 8) & 0xFF
        lo = profile_id & 0xFF
        resp = await self._send(Command.QUERY_PROFILE_LABEL, data_mid=hi, data_lo=lo)
        if resp and resp.has_data:
            return resp.data.decode("utf-8", errors="replace")
        return None

    async def query_current_profile_number(self) -> int | None:
        resp = await self._send(Command.QUERY_CURRENT_PROFILE_NUMBER)
        if resp and resp.has_data and len(resp.data) >= 2:
            return (resp.data[0] << 8) | resp.data[1]
        return None

    # ------------------------------------------------------------------
    # DALI device info
    # ------------------------------------------------------------------

    async def query_control_gear_addresses(self) -> list[int]:
        """Return list of short addresses (0-63) present in the database."""
        resp = await self._send(Command.QUERY_CONTROL_GEAR_DALI_ADDRESSES)
        if not (resp and resp.has_data and len(resp.data) == 8):
            return []
        addresses = []
        for byte_idx, byte_val in enumerate(resp.data):
            for bit in range(8):
                if byte_val & (1 << bit):
                    addresses.append(byte_idx * 8 + bit)
        return addresses

    async def query_device_label(self, address: int) -> str | None:
        """address: 0-63 for CG, 64-127 for CD"""
        resp = await self._send(Command.QUERY_DALI_DEVICE_LABEL, address=address)
        if resp and resp.has_data:
            return resp.data.decode("utf-8", errors="replace")
        return None

    async def query_cg_type(self, address: int) -> DaliCgTypeMask:
        """Return the 32-bit device type bitmask for address 0-63."""
        resp = await self._send(Command.DALI_QUERY_CG_TYPE, address=address)
        if resp and resp.has_data and len(resp.data) >= 4:
            value = (
                resp.data[0]
                | (resp.data[1] << 8)
                | (resp.data[2] << 16)
                | (resp.data[3] << 24)
            )
            try:
                return DaliCgTypeMask(value)
            except ValueError:
                return DaliCgTypeMask(value & 0x3FFFF)
        return DaliCgTypeMask(0)

    async def query_colour_features(self, address: int) -> DeviceColourFeatures:
        resp = await self._send(Command.QUERY_DALI_COLOUR_FEATURES, address=address)
        if resp and resp.has_data:
            parsed = parse_colour_features(resp.data[0])
            return DeviceColourFeatures(
                xy=parsed["xy"],
                tc=parsed["tc"],
                primaries=parsed["primaries"],
                rgbwaf_channels=parsed["rgbwaf_channels"],
            )
        return DeviceColourFeatures()

    async def query_colour_temp_limits(self, address: int) -> ColourTempLimits | None:
        resp = await self._send(Command.QUERY_DALI_COLOUR_TEMP_LIMITS, address=address)
        if resp and resp.has_data and len(resp.data) >= 10:
            d = resp.data
            return ColourTempLimits(
                physical_warmest_k=(d[0] << 8) | d[1],
                physical_coolest_k=(d[2] << 8) | d[3],
                soft_warmest_k=(d[4] << 8) | d[5],
                soft_coolest_k=(d[6] << 8) | d[7],
                step_k=(d[8] << 8) | d[9],
            )
        return None

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    async def query_level(self, address: int) -> int | None:
        """Return arc level 0-254 (or ARC_LEVEL_MIXED=255 for mixed groups)."""
        resp = await self._send(Command.DALI_QUERY_LEVEL, address=address)
        if resp and resp.has_data:
            return resp.data[0]
        return None

    async def query_colour(self, address: int) -> ColourState | None:
        resp = await self._send(Command.QUERY_DALI_COLOUR, address=address)
        if not (resp and resp.has_data and len(resp.data) >= 1):
            return None
        try:
            colour_type = ColourType(resp.data[0])
        except ValueError:
            return None
        return parse_colour_payload(colour_type, resp.data[1:])

    async def query_control_gear_status(self, address: int) -> DaliStatusMask:
        resp = await self._send(Command.DALI_QUERY_CONTROL_GEAR_STATUS, address=address)
        if resp and resp.has_data:
            try:
                return DaliStatusMask(resp.data[0])
            except ValueError:
                return DaliStatusMask(resp.data[0] & 0xFF)
        return DaliStatusMask(0)

    # ------------------------------------------------------------------
    # Lighting control commands
    # ------------------------------------------------------------------

    async def set_arc_level(self, address: int, level: int) -> bool:
        """Set arc level 0-254 on address (CG, group, or broadcast)."""
        resp = await self._send(Command.DALI_ARC_LEVEL, address=address, data_lo=level)
        return resp is not None and not resp.is_error

    async def set_off(self, address: int) -> bool:
        resp = await self._send(Command.DALI_OFF, address=address)
        return resp is not None and not resp.is_error

    async def recall_max(self, address: int) -> bool:
        resp = await self._send(Command.DALI_RECALL_MAX, address=address)
        return resp is not None and not resp.is_error

    async def recall_min(self, address: int) -> bool:
        resp = await self._send(Command.DALI_RECALL_MIN, address=address)
        return resp is not None and not resp.is_error

    async def recall_scene(self, address: int, scene: int) -> bool:
        """Recall a scene on address. address 64-79 targets a group."""
        resp = await self._send(Command.DALI_SCENE, address=address, data_lo=scene)
        return resp is not None and not resp.is_error

    async def custom_fade(self, address: int, level: int, seconds: int) -> bool:
        """Fade to level over a custom duration in seconds."""
        hi = (seconds >> 8) & 0xFF
        lo = seconds & 0xFF
        resp = await self._send(
            Command.DALI_CUSTOM_FADE,
            address=address,
            data_hi=level,
            data_mid=hi,
            data_lo=lo,
        )
        return resp is not None and not resp.is_error

    async def enable_dapc_sequence(self, address: int) -> bool:
        """Begin a DAPC (Direct Arc Power Control) sequence for immediate level setting."""
        resp = await self._send(Command.DALI_ENABLE_DAPC_SEQ, address=address)
        return resp is not None and not resp.is_error

    async def stop_fade(self, address: int) -> bool:
        resp = await self._send(Command.DALI_STOP_FADE, address=address)
        return resp is not None and not resp.is_error

    async def inhibit(self, address: int, seconds: int) -> bool:
        hi = (seconds >> 8) & 0xFF
        lo = seconds & 0xFF
        resp = await self._send(Command.DALI_INHIBIT, address=address, data_mid=hi, data_lo=lo)
        return resp is not None and not resp.is_error

    # ------------------------------------------------------------------
    # Colour control
    # ------------------------------------------------------------------

    async def set_colour_tc(
        self,
        address: int,
        kelvin: int,
        arc_level: int = 0xFF,
    ) -> bool:
        """Set colour temperature in Kelvin. arc_level 0xFF = no arc change."""
        seq = self._c.next_seq()
        data = build_tc_colour_data(kelvin)
        frame = build_dali_colour_frame(seq, address, arc_level, ColourType.TC, data)
        try:
            resp = await self._c.send(frame, seq)
            return not resp.is_error
        except (asyncio.TimeoutError, ConnectionError):
            return False

    async def set_colour_rgb(
        self,
        address: int,
        r: int, g: int, b: int,
        w: int = 0xFF, a: int = 0xFF, f: int = 0xFF,
        arc_level: int = 0xFF,
    ) -> bool:
        """Set RGBWAF colour. 0xFF for unused channels = no change."""
        seq = self._c.next_seq()
        data = build_rgbwaf_colour_data(r, g, b, w, a, f)
        frame = build_dali_colour_frame(seq, address, arc_level, ColourType.RGBWAF, data)
        try:
            resp = await self._c.send(frame, seq)
            return not resp.is_error
        except (asyncio.TimeoutError, ConnectionError):
            return False

    async def set_colour_xy(
        self,
        address: int,
        x: int,
        y: int,
        arc_level: int = 0xFF,
    ) -> bool:
        """Set CIE 1931 XY colour (0–0xFFFE; 0xFFFF = no change)."""
        seq = self._c.next_seq()
        data = build_xy_colour_data(x, y)
        frame = build_dali_colour_frame(seq, address, arc_level, ColourType.XY, data)
        try:
            resp = await self._c.send(frame, seq)
            return not resp.is_error
        except (asyncio.TimeoutError, ConnectionError):
            return False

    # ------------------------------------------------------------------
    # Profile control
    # ------------------------------------------------------------------

    async def change_profile(self, profile_id: int) -> bool:
        """Request a profile change. 0xFFFF = revert to schedule."""
        hi = (profile_id >> 8) & 0xFF
        lo = profile_id & 0xFF
        resp = await self._send(Command.CHANGE_PROFILE_NUMBER, data_mid=hi, data_lo=lo)
        return resp is not None and resp.ok

    # ------------------------------------------------------------------
    # TPI event configuration
    # ------------------------------------------------------------------

    async def query_event_emit_state(self) -> int | None:
        """Return the raw TPI event mode byte, or None on failure."""
        resp = await self._send(Command.QUERY_TPI_EVENT_EMIT_STATE)
        if resp and resp.has_data:
            return resp.data[0]
        return None

    async def enable_events_unicast(self, mode_flags: int) -> bool:
        """Enable TPI events with the given TpiEventMode flags."""
        resp = await self._send(Command.ENABLE_TPI_EVENT_EMIT, address=mode_flags)
        return resp is not None and resp.ok

    async def set_unicast_address(self, ip: str, port: int) -> bool:
        """Configure the controller to send events via unicast to ip:port."""
        seq = self._c.next_seq()
        frame = build_unicast_address_frame(seq, ip, port)
        try:
            resp = await self._c.send(frame, seq)
            return resp.ok
        except (asyncio.TimeoutError, ConnectionError):
            return False

    async def configure_unicast_events(self, ha_ip: str, ha_port: int) -> bool:
        """Full unicast setup: set address then enable unicast + events."""
        ok = await self.set_unicast_address(ha_ip, ha_port)
        if not ok:
            _LOGGER.warning("Failed to set unicast address %s:%d", ha_ip, ha_port)
            return False
        ok = await self.enable_events_unicast(TpiEventMode.ENABLE_UNICAST_MODE | TpiEventMode.ENABLED)
        if not ok:
            _LOGGER.warning("Failed to enable unicast events")
        return ok
