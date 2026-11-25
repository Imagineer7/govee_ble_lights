"""Actions under govee_ble_lights.* for segment control and presets."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID, ENTITY_MATCH_ALL
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
import homeassistant.helpers.config_validation as cv

from .const import SERVICE_DOMAIN, DATA_BLE_LIGHTS, DATA_PRESETS

RGB_SCHEMA = vol.All(vol.Coerce(tuple), vol.ExactSequence((cv.byte, cv.byte, cv.byte)))
SEGMENT_NUMBER = vol.All(vol.Coerce(int), vol.Range(min=1, max=16))

SET_SEGMENT_COLOR_SCHEMA = cv.make_entity_service_schema({
    vol.Required("segments"): vol.All(cv.ensure_list, [SEGMENT_NUMBER]),
    vol.Required("rgb_color"): RGB_SCHEMA,
})
SET_SEGMENTS_SCHEMA = cv.make_entity_service_schema({
    vol.Required("colors"): vol.All(cv.ensure_list, vol.Length(min=1, max=16), [vol.Any(None, RGB_SCHEMA)]),
})
APPLY_PRESET_SCHEMA = cv.make_entity_service_schema({
    vol.Required("preset"): cv.string,
})
SAVE_PRESET_SCHEMA = vol.Schema({
    vol.Required("name"): vol.All(cv.string, vol.Strip, vol.Length(min=1, max=40)),
    vol.Optional("colors"): vol.All(cv.ensure_list, vol.Length(min=1, max=16), [RGB_SCHEMA]),
    vol.Optional(ATTR_ENTITY_ID): cv.entity_id,
})
DELETE_PRESET_SCHEMA = vol.Schema({
    vol.Required("name"): cv.string,
})


def _target_lights(hass: HomeAssistant, call: ServiceCall) -> list:
    lights = [l for l in hass.data.get(DATA_BLE_LIGHTS, {}).values() if l.segment_count and l.entity_id]
    wanted = call.data.get(ATTR_ENTITY_ID)
    if wanted == ENTITY_MATCH_ALL:
        return lights
    wanted = wanted or []
    found = [l for l in lights if l.entity_id in wanted]
    if not found:
        raise ServiceValidationError(
            "Target a segmented Govee BLE strip by entity_id (the main strip light, not a segment)")
    return found


def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(SERVICE_DOMAIN, "set_segment_color"):
        return

    async def set_segment_color(call: ServiceCall) -> None:
        for light in _target_lights(hass, call):
            await light.async_set_segment_color(call.data["segments"], call.data["rgb_color"])

    async def set_segments(call: ServiceCall) -> None:
        for light in _target_lights(hass, call):
            await light.async_set_segments(call.data["colors"])

    async def apply_preset(call: ServiceCall) -> None:
        name = call.data["preset"]
        if name not in hass.data[DATA_PRESETS].presets:
            raise ServiceValidationError(f"No preset named '{name}'")
        for light in _target_lights(hass, call):
            await light.async_apply_preset(name)

    async def save_preset(call: ServiceCall) -> None:
        colors = call.data.get("colors")
        if colors is None:
            entity_id = call.data.get(ATTR_ENTITY_ID)
            light = next((l for l in hass.data.get(DATA_BLE_LIGHTS, {}).values()
                          if l.entity_id == entity_id), None)
            if light is None:
                raise ServiceValidationError("Give either 'colors' or the 'entity_id' of a strip to copy from")
            colors = [list(c) if c is not None else [0, 0, 0] for c in light.segment_colors]
        await hass.data[DATA_PRESETS].async_save_preset(call.data["name"], [list(c) for c in colors])

    async def delete_preset(call: ServiceCall) -> None:
        if not await hass.data[DATA_PRESETS].async_delete_preset(call.data["name"]):
            raise ServiceValidationError(f"No preset named '{call.data['name']}'")

    hass.services.async_register(SERVICE_DOMAIN, "set_segment_color", set_segment_color,
                                 schema=SET_SEGMENT_COLOR_SCHEMA)
    hass.services.async_register(SERVICE_DOMAIN, "set_segments", set_segments, schema=SET_SEGMENTS_SCHEMA)
    hass.services.async_register(SERVICE_DOMAIN, "apply_preset", apply_preset, schema=APPLY_PRESET_SCHEMA)
    hass.services.async_register(SERVICE_DOMAIN, "save_preset", save_preset, schema=SAVE_PRESET_SCHEMA)
    hass.services.async_register(SERVICE_DOMAIN, "delete_preset", delete_preset, schema=DELETE_PRESET_SCHEMA)
