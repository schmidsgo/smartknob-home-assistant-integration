"""Define the services called by smartknob on HASS entities."""

from enum import Enum
import json

from homeassistant.core import HomeAssistant, State

from .logger import _LOGGER


def mired_to_kelvin(mired: int) -> int:
    """Convert a color temperature in mireds to Kelvin."""
    return round(1000000 / mired)


def kelvin_to_mired(kelvin: int | None) -> int | None:
    """Convert a color temperature in Kelvin to mireds, the unit the knob uses."""
    if not kelvin:
        return None
    return round(1000000 / kelvin)


class SwitchState:
    """Defines the structure of the SwitchState object."""

    def __init__(self, on: bool) -> None:
        """Initialize the SwitchState object."""
        self.on: bool = on


class LightState:
    """Defines the structure of the LightState object."""

    def __init__(
        self,
        on: bool | None,
        brightness: int | None,
        color_temp: int | None,
        rgb_color: list[int] | None,
    ) -> None:
        """Initialize the LightState object."""
        self.on: bool = on
        self.brightness: int = brightness
        self.color_temp: int = color_temp
        self.rgb_color: list[int] = rgb_color


class BlindsState:
    """Defines the structure of the BlindsState object."""

    def __init__(self, position: int) -> None:
        """Initialize the BlindsState object."""
        self.position: int = position


class ClimateMode(Enum):
    """Enum for climate modes."""

    off = 0
    heat = 1
    cool = 2
    heat_cool = 3
    auto = 4
    dry = 5
    fan_only = 6


class ClimateState:
    """Defines the structure of the ClimateState object."""

    def __init__(self, mode: int, target_temp: int, current_temp: int) -> None:
        """Initialize the ClimateState object."""
        self.mode: int = mode
        self.target_temp: int = target_temp
        self.current_temp: int = current_temp


class MediaState:
    """Defines the structure of the MediaState object."""

    def __init__(self, state) -> None:
        """Initialize the MediaState object."""
        self.volume = state["volume"]
        self.mute = state["mute"]
        self.playing = state["playing"]
        self.previous = state["previous"]
        self.next = state["next"]


# The knob's text font only has Latin-1; map common typographic characters to it.
_KNOB_TEXT_REPLACEMENTS = str.maketrans(
    {"\u2013": "-", "\u2014": "-", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2026": "..."}
)


def _truncate_utf8(text: str | None, max_bytes: int) -> str:
    """Shorten text to max_bytes of UTF-8 without cutting a character in half."""
    if not text:
        return ""
    text = str(text).translate(_KNOB_TEXT_REPLACEMENTS)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip() + "..."


class MediaPlayerState:
    """State of a media_player entity as the knob's media player app shows it.

    The knob copies the whole state into a 256 byte buffer, so title and artist
    are shortened to keep the JSON well below that.
    """

    TITLE_MAX_BYTES = 64
    ARTIST_MAX_BYTES = 40

    def __init__(
        self,
        state: str,
        volume: int | None,
        muted: bool,
        title: str | None,
        artist: str | None,
    ) -> None:
        """Initialize the MediaPlayerState object."""
        self.state: str = state
        self.volume: int | None = volume
        self.muted: bool = muted
        self.title: str = _truncate_utf8(title, self.TITLE_MAX_BYTES)
        self.artist: str = _truncate_utf8(artist, self.ARTIST_MAX_BYTES)

    @classmethod
    def from_hass(cls, state: State) -> "MediaPlayerState":
        """Build the knob state from a Home Assistant media_player state."""
        attributes = state.attributes
        volume_level = attributes.get("volume_level")
        volume = round(volume_level * 100) if volume_level is not None else None
        artist = attributes.get("media_artist") or attributes.get("media_album_artist")
        title = attributes.get("media_title") or attributes.get("media_channel")
        return cls(
            state.state,
            volume,
            bool(attributes.get("is_volume_muted")),
            title,
            artist,
        )

    def as_dict(self) -> dict:
        """Return the JSON-serialisable form sent to the knob."""
        data = {
            "state": self.state,
            "muted": self.muted,
            "title": self.title,
            "artist": self.artist,
        }
        if self.volume is not None:
            data["volume"] = self.volume
        return data


# Actions the knob's media player app can send, mapped to media_player services.
MEDIA_PLAYER_ACTIONS = {
    "play": "media_play",
    "pause": "media_pause",
    "play_pause": "media_play_pause",
    "next": "media_next_track",
    "previous": "media_previous_track",
    "mute": "volume_mute",
    "unmute": "volume_mute",
}


class LockState:
    """Defines the structure of the LockState object."""

    def __init__(self, state) -> None:
        """Initialize the LockState object."""
        self.locked = state["locked"]


class Services:
    """Handles services."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the Service Handler."""
        self.hass = hass

    async def async_toggle_switch(self, entity_id: str, state: SwitchState):
        """Switch the entity on or off."""
        # Use the entity's own domain: the light_switch app controls light.* entities,
        # which reject switch.turn_on.
        domain = entity_id.split(".", 1)[0]
        if state.on:
            await self.hass.async_create_task(
                self.hass.services.async_call(
                    domain, "turn_on", {"entity_id": entity_id}
                )
            )
        elif not state.on:
            await self.hass.async_create_task(
                self.hass.services.async_call(
                    domain, "turn_off", {"entity_id": entity_id}
                )
            )
        else:
            _LOGGER.error("Not implemented")

    async def async_set_light(self, entity_id: str, state: LightState):
        """Switch the light on or off, and set its brightness and color."""
        if state.brightness is None or not 0 <= state.brightness <= 255:
            _LOGGER.error("Invalid brightness from knob: %s", state.brightness)
            return

        data = {"entity_id": entity_id, "brightness": state.brightness}
        if state.rgb_color:
            data["rgb_color"] = state.rgb_color
        elif state.color_temp:
            # The knob speaks mireds; Home Assistant only accepts Kelvin since the
            # color_temp (mired) service field was removed.
            data["color_temp_kelvin"] = mired_to_kelvin(state.color_temp)

        await self.hass.async_create_task(
            self.hass.services.async_call("light", "turn_on", data, True)
        )

    async def async_handle_blinds(self, entity_id: str, position: int):
        """Handle blinds entity."""
        await self.hass.async_create_task(
            self.hass.services.async_call(
                "cover",
                "set_cover_position",
                {
                    "entity_id": entity_id,
                    "position": position,
                },
            )
        )

    async def async_handle_climate(self, entity_id: str, state: ClimateState):
        """Handle climate entity."""
        mode = ClimateMode(state.mode)

        await self.hass.async_create_task(
            self.hass.services.async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": entity_id,
                    "temperature": state.target_temp,
                },
            )
        )
        await self.hass.async_create_task(
            self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {
                    "entity_id": entity_id,
                    "hvac_mode": mode.name,
                },
            )
        )


    async def async_handle_media_player(self, entity_id: str, state: dict):
        """Handle a volume change or a button action from the media player app."""
        if state.get("volume") is not None:
            try:
                volume = min(max(float(state["volume"]), 0), 100)
            except (TypeError, ValueError):
                _LOGGER.error("Invalid volume from knob: %s", state.get("volume"))
                return
            await self.hass.services.async_call(
                "media_player",
                "volume_set",
                {"entity_id": entity_id, "volume_level": round(volume / 100, 2)},
            )

        action = state.get("action")
        if action is None:
            return
        service = MEDIA_PLAYER_ACTIONS.get(action)
        if service is None:
            _LOGGER.error("Unknown media player action from knob: %s", action)
            return

        data = {"entity_id": entity_id}
        if service == "volume_mute":
            data["is_volume_muted"] = action == "mute"
        elif action == "play":
            current = self.hass.states.get(entity_id)
            if current is not None and current.state == "off":
                # A switched off player ignores media_play, wake it up instead.
                service = "turn_on"

        await self.hass.services.async_call("media_player", service, data)


class StateEncoder(json.JSONEncoder):
    """Custom JSON encoder for the state objects."""

    def default(self, o):
        """Encode the state objects."""
        if isinstance(o, SwitchState):
            return {
                "on": o.on,
            }
        if isinstance(o, LightState):
            return {
                "on": o.on,
                "brightness": o.brightness,
                "color_temp": o.color_temp,
                "rgb_color": o.rgb_color,
            }
        if isinstance(o, BlindsState):
            return {
                "position": o.position,
            }
        if isinstance(o, MediaPlayerState):
            return o.as_dict()
        if isinstance(o, ClimateState):
            return {
                "mode": o.mode,
                "target_temp": o.target_temp,
                "current_temp": o.current_temp,
            }
        return super().default(o)
