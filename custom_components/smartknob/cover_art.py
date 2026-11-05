"""Album covers for the knob's media player app.

The knob cannot decode JPEG/PNG covers itself, so this view fetches the cover of a
media_player entity, crops it to the round 240x240 display and returns raw RGB565
pixels (LVGL's LV_IMG_CF_TRUE_COLOR at 16 bit colour depth, little endian).

The knob is not logged in to Home Assistant, so the view does not require auth.
Instead every URL carries a random key that changes with each Home Assistant start.
"""

from __future__ import annotations

import hashlib
import io
import secrets

from aiohttp import web
from PIL import Image, ImageOps

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN
from .logger import _LOGGER

COVER_SIZE = 240
COVER_URL_PATH = "/api/smartknob/cover"
# Kept outside hass.data[DOMAIN], which is dropped when the entry is removed,
# because a view can only be registered once per run.
_DATA_KEY = f"{DOMAIN}_cover_art"


def _data(hass: HomeAssistant) -> dict:
    if _DATA_KEY not in hass.data:
        hass.data[_DATA_KEY] = {"key": secrets.token_hex(8), "cache": {}}
    return hass.data[_DATA_KEY]


@callback
def async_register_cover_view(hass: HomeAssistant) -> None:
    """Register the cover view once per Home Assistant run."""
    data = _data(hass)
    if not data.get("registered"):
        hass.http.register_view(SmartknobCoverView)
        data["registered"] = True


@callback
def async_cover_url(hass: HomeAssistant, state: State) -> str:
    """Return the URL the knob loads the cover from, or "" if there is none."""
    picture = state.attributes.get("entity_picture")
    if not picture or state.state in ("off", "unavailable", "unknown"):
        return ""
    try:
        base = get_url(hass, allow_external=False, allow_cloud=False)
    except NoURLAvailableError:
        _LOGGER.debug("No internal URL, the knob can't load covers")
        return ""
    version = hashlib.sha1(picture.encode()).hexdigest()[:8]
    return (
        f"{base}{COVER_URL_PATH}/{state.entity_id}"
        f"?k={_data(hass)['key']}&v={version}"
    )


def _to_rgb565(image_bytes: bytes) -> bytes:
    """Crop and scale a cover to the display and convert it to RGB565."""
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = ImageOps.fit(
            image.convert("RGB"), (COVER_SIZE, COVER_SIZE), Image.Resampling.LANCZOS
        )
        rgb = image.tobytes()

    out = bytearray(COVER_SIZE * COVER_SIZE * 2)
    o = 0
    for i in range(0, len(rgb), 3):
        value = ((rgb[i] & 0xF8) << 8) | ((rgb[i + 1] & 0xFC) << 3) | (rgb[i + 2] >> 3)
        out[o] = value & 0xFF
        out[o + 1] = value >> 8
        o += 2
    return bytes(out)


async def _async_fetch_cover(hass: HomeAssistant, state: State) -> bytes | None:
    """Get the original cover image of a media_player entity."""
    component = hass.data.get("media_player")
    entity = component.get_entity(state.entity_id) if component else None
    if entity is not None:
        image, _content_type = await entity.async_get_media_image()
        if image:
            return image

    picture = state.attributes.get("entity_picture", "")
    if picture.startswith("http"):
        session = async_get_clientsession(hass)
        async with session.get(picture, timeout=10) as response:
            if response.status == 200:
                return await response.read()
    return None


class SmartknobCoverView(HomeAssistantView):
    """Serve the current cover of a media_player as raw RGB565 pixels."""

    url = COVER_URL_PATH + "/{entity_id}"
    name = "api:smartknob:cover"
    requires_auth = False

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        """Return the cover."""
        hass: HomeAssistant = request.app["hass"]
        data = _data(hass)
        if not secrets.compare_digest(request.query.get("k", ""), data["key"]):
            return web.Response(status=403)
        if not entity_id.startswith("media_player."):
            return web.Response(status=404)

        state = hass.states.get(entity_id)
        if state is None:
            return web.Response(status=404)

        version = hashlib.sha1(
            state.attributes.get("entity_picture", "").encode()
        ).hexdigest()[:8]
        cached = data["cache"].get(entity_id)
        if cached is None or cached[0] != version:
            try:
                image = await _async_fetch_cover(hass, state)
                if image is None:
                    return web.Response(status=404)
                pixels = await hass.async_add_executor_job(_to_rgb565, image)
            except Exception as err:  # noqa: BLE001 - any failure just means no cover
                _LOGGER.warning("Could not prepare cover for %s: %s", entity_id, err)
                return web.Response(status=502)
            cached = (version, pixels)
            data["cache"][entity_id] = cached

        return web.Response(body=cached[1], content_type="application/octet-stream")
