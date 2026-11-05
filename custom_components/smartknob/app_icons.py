"""Menu icons for the knob.

Every app on the knob shows an icon in the main menu: 80x80 for the selected app,
40x40 for its neighbours. The icon is the one chosen in the SmartKnob panel; if none
was chosen, the entity's own icon in Home Assistant is used.

The knob cannot render SVG or fonts at runtime, so this view draws the Material Design
Icons glyph with Pillow and returns raw LVGL LV_IMG_CF_ALPHA_4BIT pixels: the 80x80
image (3200 bytes) followed by the 40x40 one (800 bytes). The knob recolours them like
its built-in icons.

Like the cover view, the knob isn't logged in, so URLs carry the random key from
cover_art instead of requiring auth. Only icon names are served, never entity data.
"""

from __future__ import annotations

import json
import os
import re
import secrets

from aiohttp import web
from PIL import Image, ImageDraw, ImageFont

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.icon import async_get_icons

from .const import DOMAIN
from .cover_art import _data as _cover_data
from .cover_art import _knob_base_url
from .logger import _LOGGER

ICON_URL_PATH = "/api/smartknob/icon"
ICON_SIZES = (80, 40)
ICON_BYTES = sum((size + 1) // 2 * size for size in ICON_SIZES)

_MDI_DIR = os.path.join(os.path.dirname(__file__), "mdi")
_FONT_FILE = os.path.join(_MDI_DIR, "materialdesignicons-webfont.ttf")
_CODEPOINTS_FILE = os.path.join(_MDI_DIR, "mdi_codepoints.json")
_DATA_KEY = f"{DOMAIN}_app_icons"
_NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")

# Used when neither the panel nor the entity names an icon.
_SLUG_DEFAULT_ICONS = {
    "light_switch": "mdi:lightbulb",
    "light_dimmer": "mdi:lightbulb",
    "switch": "mdi:toggle-switch-variant",
    "blinds": "mdi:blinds",
    "climate": "mdi:thermostat",
    "media_player": "mdi:speaker",
    "spotify": "mdi:spotify",
    "stopwatch": "mdi:timer-outline",
}


def _data(hass: HomeAssistant) -> dict:
    if _DATA_KEY not in hass.data:
        hass.data[_DATA_KEY] = {"cache": {}, "codepoints": None, "fonts": {}}
    return hass.data[_DATA_KEY]


@callback
def async_register_icon_view(hass: HomeAssistant) -> None:
    """Register the icon view once per Home Assistant run."""
    data = _data(hass)
    if not data.get("registered"):
        hass.http.register_view(SmartknobIconView)
        data["registered"] = True


def _mdi_name(icon: str | None) -> str | None:
    """Return the bare MDI name of "mdi:foo" / "hass:foo", or None."""
    if not icon or ":" not in icon:
        return None
    prefix, name = icon.split(":", 1)
    if prefix not in ("mdi", "hass") or not _NAME_RE.match(name):
        return None
    return name


async def _async_entity_icon(hass: HomeAssistant, entity_id: str) -> str | None:
    """Find the icon Home Assistant shows for an entity, as far as the backend knows it."""
    if not entity_id:
        return None
    entry = er.async_get(hass).async_get(entity_id)
    state = hass.states.get(entity_id)

    for icon in (
        entry.icon if entry else None,
        state.attributes.get("icon") if state else None,
        entry.original_icon if entry else None,
    ):
        if _mdi_name(icon):
            return icon

    domain = entity_id.split(".", 1)[0]
    try:
        if entry is not None and entry.translation_key:
            icons = await async_get_icons(hass, "entity", [entry.platform])
            icon = (
                icons.get(entry.platform, {})
                .get(domain, {})
                .get(entry.translation_key, {})
                .get("default")
            )
            if _mdi_name(icon):
                return icon

        icons = (await async_get_icons(hass, "entity_component", [domain])).get(
            domain, {}
        )
        device_class = (state.attributes.get("device_class") if state else None) or (
            entry.device_class or entry.original_device_class if entry else None
        )
        for key in (device_class, "_"):
            icon = icons.get(key, {}).get("default") if key else None
            if _mdi_name(icon):
                return icon
    except Exception as err:  # noqa: BLE001 - fall back to the app's default icon
        _LOGGER.debug("No icon translations for %s: %s", entity_id, err)
    return None


async def async_app_icon_url(hass: HomeAssistant, app: dict) -> str:
    """Return the URL the knob loads an app's menu icon from, or "" for its built-in one."""
    name = _mdi_name(app.get("icon"))
    if name is None:
        name = _mdi_name(await _async_entity_icon(hass, app.get("entity_id", "")))
    if name is None:
        name = _mdi_name(_SLUG_DEFAULT_ICONS.get(app.get("app_slug", "")))
    if name is None:
        return ""
    base = _knob_base_url(hass)
    if base is None:
        return ""
    return f"{base}{ICON_URL_PATH}/{name}?k={_cover_data(hass)['key']}"


def _load_codepoints() -> dict[str, int]:
    with open(_CODEPOINTS_FILE, encoding="utf-8") as file:
        return json.load(file)


def _render(name: str, codepoint: int, fonts: dict) -> bytes:
    """Draw one glyph at every size and pack it as LVGL ALPHA_4BIT."""
    out = bytearray()
    for size in ICON_SIZES:
        font = fonts.get(size)
        if font is None:
            font = fonts[size] = ImageFont.truetype(_FONT_FILE, size)
        image = Image.new("L", (size, size), 0)
        ImageDraw.Draw(image).text(
            (size / 2, size / 2), chr(codepoint), font=font, fill=255, anchor="mm"
        )
        pixels = image.tobytes()
        stride = (size + 1) // 2
        for y in range(size):
            row = pixels[y * size : (y + 1) * size]
            packed = bytearray(stride)
            for x, value in enumerate(row):
                nibble = (value + 8) // 17  # 0..255 -> 0..15
                if x % 2 == 0:
                    packed[x // 2] = nibble << 4
                else:
                    packed[x // 2] |= nibble
            out += packed
    return bytes(out)


class SmartknobIconView(HomeAssistantView):
    """Serve an MDI icon as raw LVGL alpha pixels for the knob's menu."""

    url = ICON_URL_PATH + "/{name}"
    name = "api:smartknob:icon"
    requires_auth = False

    async def get(self, request: web.Request, name: str) -> web.Response:
        """Return the icon."""
        hass: HomeAssistant = request.app["hass"]
        if not secrets.compare_digest(
            request.query.get("k", ""), _cover_data(hass)["key"]
        ):
            return web.Response(status=403)
        if not _NAME_RE.match(name):
            return web.Response(status=404)

        data = _data(hass)
        pixels = data["cache"].get(name)
        if pixels is None:
            if data["codepoints"] is None:
                data["codepoints"] = await hass.async_add_executor_job(
                    _load_codepoints
                )
            codepoint = data["codepoints"].get(name)
            if codepoint is None:
                return web.Response(status=404)
            try:
                pixels = await hass.async_add_executor_job(
                    _render, name, codepoint, data["fonts"]
                )
            except Exception as err:  # noqa: BLE001 - the knob keeps its own icon
                _LOGGER.warning("Could not draw icon %s: %s", name, err)
                return web.Response(status=500)
            data["cache"][name] = pixels

        return web.Response(body=pixels, content_type="application/octet-stream")
