from __future__ import annotations

import array
import asyncio
import base64
import json
import logging
import re
from datetime import timedelta
from enum import IntEnum
from pathlib import Path

import bleak_retry_connector
from bleak import BleakClient
from homeassistant.components import bluetooth
from homeassistant.components.light import (ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_EFFECT, ColorMode, LightEntity,
                                            LightEntityFeature, ATTR_COLOR_TEMP_KELVIN)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
import homeassistant.util.color as color_util

from . import Hub
from .const import (DOMAIN, SEGMENTED_MODELS, DATA_BLE_LIGHTS, DATA_PRESET_SELECTS, DATA_PRESETS,
                    segment_count_for, build_device_info)
from .govee_utils import prepareMultiplePacketsData
from .presets import resample

SCAN_INTERVAL = timedelta(seconds=30)

_LOGGER = logging.getLogger(__name__)

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'
EFFECT_PARSE = re.compile(r"\[(\d+)/(\d+)/(\d+)/(\d+)]")

RGBTuple = tuple[int, int, int]


class LedCommand(IntEnum):
    """ A control command packet's type. """
    POWER = 0x01
    BRIGHTNESS = 0x04
    COLOR = 0x05


class LedMode(IntEnum):
    """
    The mode in which a color change happens in.

    Currently only manual is supported.
    """
    MANUAL = 0x02
    MICROPHONE = 0x06
    SCENES = 0x05
    SEGMENTS = 0x15


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        return

    if hub.devices is not None:
        devices = hub.devices
        for device in devices:
            if device['type'] == 'devices.types.light':
                _LOGGER.info("Adding device: %s", device)
                async_add_entities([GoveeAPILight(hub, device)])
    elif hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, hub.address.upper(), False)
        light = GoveeBluetoothLight(hub, ble_device, config_entry)
        entities: list[LightEntity] = [light]
        if light.segment_count:
            light.segment_entities = [GoveeSegmentLight(light, i) for i in range(light.segment_count)]
            entities.extend(light.segment_entities)
        async_add_entities(entities)


class GoveeAPILight(LightEntity, dict):
    _attr_color_mode = ColorMode.RGB

    def __init__(self, hub: Hub, device: dict) -> None:
        """Initialize an API light."""
        super().__init__()

        self.hub = hub

        self._state = None
        self._brightness = None

        self.device_data = device
        self.sku = self.device_data["sku"]
        self.device = self.device_data["device"]

        self._attr_name = device["deviceName"]

        color_modes: set[ColorMode] = set()

        for cap in device["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                color_modes.add(ColorMode.ONOFF)
            if cap['instance'] == 'brightness':
                color_modes.add(ColorMode.BRIGHTNESS)
            if cap['instance'] == 'colorTemperatureK':
                color_modes.add(ColorMode.COLOR_TEMP)
                self._attr_min_color_temp_kelvin = cap['parameters']['range']['min']
                self._attr_max_color_temp_kelvin = cap['parameters']['range']['max']
                self._attr_min_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_min_color_temp_kelvin)
                self._attr_max_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_max_color_temp_kelvin)
            if cap['instance'] == 'colorRgb':
                color_modes.add(ColorMode.RGB)
            if cap['instance'] == 'lightScene':
                self._attr_supported_features = LightEntityFeature(
                    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
                )

        if ColorMode.ONOFF in color_modes:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        if ColorMode.BRIGHTNESS in color_modes:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        if ColorMode.COLOR_TEMP in color_modes:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        if ColorMode.RGB in color_modes:
            self._attr_supported_color_modes = {ColorMode.RGB}

        self._state = None
        self._brightness = None
        self.update_scenes()

    async def async_update(self):
        """Retrieve latest state."""
        _LOGGER.info("Updating device: %s", self.device_data)

        state = await self.hub.api.get_device_state(self.sku, self.device)
        for cap in state["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                self._state = cap['state']['value'] == 1
            if cap['instance'] == 'brightness':
                self._brightness = cap['state']['value']
            if cap['instance'] == 'colorTemperatureK':
                value = cap['state']['value']
                if value != 0:
                    self._attr_color_temp_kelvin = value
                    self._attr_color_temp = color_util.color_temperature_kelvin_to_mired(value)
            if cap['instance'] == 'colorRgb':
                num = cap['state']['value']
                self._attr_rgb_color = ((num >> 16) & 0xFF, (num >> 8) & 0xFF, num & 0xFF)

    async def update_scenes(self):
        if LightEntityFeature.EFFECT in self.supported_features:
            if self._attr_effect_list is None or len(self._attr_effect_list) == 0:
                _LOGGER.info("Updating device effects: %s", self.device_data)

                store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
                scenes = await self.hub.api.list_scenes(self.sku, self.device)

                await store.async_save(scenes)

                self._attr_effect_list = [scene['name'] for scene in scenes]

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self.device

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            await self.hub.api.set_brightness(self.sku, self.device, (brightness / 255) * 100)
            self._brightness = brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            await self.hub.api.set_color_rgb(self.sku, self.device, red, green, blue)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            await self.hub.api.set_color_temp(self.sku, self.device, kelvin)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs.get(ATTR_EFFECT)
            store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
            scenes = (
                scene for scene in await store.async_load()
                if scene['name'] == effect_name
            )
            scene = next(scenes)
            _LOGGER.info("Set scene: %s", scene)
            await self.hub.api.set_scene(self.sku, self.device, scene['value'])

        await self.hub.api.toggle_power(self.sku, self.device, 1)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.api.toggle_power(self.sku, self.device, 0)
        self._state = False


class GoveeBluetoothLight(RestoreEntity, LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature(
        LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION)
    # Card/automation helpers; no need to fill the recorder database with them.
    _unrecorded_attributes = frozenset({"segment_count", "segment_colors", "segment_entities", "preset_entity"})

    def __init__(self, hub: Hub, ble_device, config_entry: ConfigEntry) -> None:
        """Initialize an bluetooth light."""
        self._mac = hub.address
        self._model = config_entry.data["model"]
        self._is_segmented = self._model in SEGMENTED_MODELS
        self._ble_device = ble_device
        self._state = None
        self._brightness = None
        self._entry_id = config_entry.entry_id
        self._device_name = config_entry.title or f"Govee {self._model}"

        self.segment_count = segment_count_for(self._model)
        # Last color sent to each segment (None = unknown, e.g. after a scene)
        self.segment_colors: list[RGBTuple | None] = [None] * self.segment_count
        self.segment_entities: list[GoveeSegmentLight] = []

        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()

    @property
    def effect_list(self) -> list[str] | None:
        effect_list = []
        json_data = json.loads(Path(Path(__file__).parent / "jsons" / (self._model + ".json")).read_text())
        for categoryIdx, category in enumerate(json_data['data']['categories']):
            for sceneIdx, scene in enumerate(category['scenes']):
                for leffectIdx, lightEffect in enumerate(scene['lightEffects']):
                    for seffectIxd, specialEffect in enumerate(lightEffect['specialEffect']):
                        # if 'supportSku' not in specialEffect or self._model in specialEffect['supportSku']:
                        # Workaround cause we need to store some metadata in effect (effect names not unique)
                        indexes = str(categoryIdx) + "/" + str(sceneIdx) + "/" + str(leffectIdx) + "/" + str(
                            seffectIxd)
                        effect_list.append(
                            category['categoryName'] + " - " + scene['sceneName'] + ' - ' + lightEffect[
                                'scenceName'] + " [" + indexes + "]")

        return effect_list

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return "GOVEE Light"

    @property
    def unique_id(self) -> str:
        """Return a unique, Home Assistant friendly identifier for this entity."""
        return self._mac.replace(":", "")

    @property
    def device_info(self):
        return build_device_info(self.unique_id, self._device_name, self._model)

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._state

    @property
    def extra_state_attributes(self):
        if not self.segment_count:
            return None
        preset_select = self.hass.data.get(DATA_PRESET_SELECTS, {}).get(self._entry_id) if self.hass else None
        return {
            "segment_count": self.segment_count,
            "segment_colors": [list(c) if c is not None else None for c in self.segment_colors],
            "segment_entities": [e.entity_id for e in self.segment_entities if e.entity_id],
            "preset_entity": preset_select.entity_id if preset_select else None,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.data.setdefault(DATA_BLE_LIGHTS, {})[self._entry_id] = self

        # BLE strips can't report their state, so restore what we last sent.
        last = await self.async_get_last_state()
        if last is not None:
            if last.state in (STATE_ON, STATE_OFF):
                self._state = last.state == STATE_ON
            if isinstance(last.attributes.get(ATTR_BRIGHTNESS), int):
                self._brightness = last.attributes[ATTR_BRIGHTNESS]
            saved = last.attributes.get("segment_colors")
            if isinstance(saved, list) and len(saved) == self.segment_count:
                self.segment_colors = [tuple(c) if isinstance(c, list) and len(c) == 3 else None for c in saved]

    async def async_will_remove_from_hass(self) -> None:
        lights = self.hass.data.get(DATA_BLE_LIGHTS, {})
        if lights.get(self._entry_id) is self:
            lights.pop(self._entry_id)
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def async_turn_on(self, **kwargs) -> None:
        commands = [self._prepareSinglePacketData(LedCommand.POWER, [0x1])]
        new_segment_colors = None
        clear_preset = False

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [brightness]))

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            if self._is_segmented:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR,
                                                              [LedMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00, 0x00,
                                                               0x00, 0x00, 0xFF, 0x7F]))
                new_segment_colors = [(red, green, blue)] * self.segment_count
            else:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR, [LedMode.MANUAL, red, green, blue]))
            clear_preset = True
        if ATTR_EFFECT in kwargs:
            effect = kwargs.get(ATTR_EFFECT)
            if len(effect) > 0:
                search = EFFECT_PARSE.search(effect)

                # Parse effect indexes
                categoryIndex = int(search.group(1))
                sceneIndex = int(search.group(2))
                lightEffectIndex = int(search.group(3))
                specialEffectIndex = int(search.group(4))

                json_data = json.loads(Path(Path(__file__).parent / "jsons" / (self._model + ".json")).read_text())
                category = json_data['data']['categories'][categoryIndex]
                scene = category['scenes'][sceneIndex]
                lightEffect = scene['lightEffects'][lightEffectIndex]
                specialEffect = lightEffect['specialEffect'][specialEffectIndex]

                # Prepare packets to send big payload in separated chunks
                for command in prepareMultiplePacketsData(0xa3,
                                                          array.array('B', [0x02]),
                                                          array.array('B',
                                                                      base64.b64decode(specialEffect['scenceParam'])
                                                                      )):
                    commands.append(command)
                # Scene colors aren't known to us
                new_segment_colors = [None] * self.segment_count
                clear_preset = True

        await self._async_send(commands)

        self._state = True
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs[ATTR_BRIGHTNESS]
        if new_segment_colors is not None and self.segment_count:
            self.segment_colors = new_segment_colors
        if clear_preset:
            self._set_current_preset(None)
        self._async_segments_changed()

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_send([self._prepareSinglePacketData(LedCommand.POWER, [0x0])])
        self._state = False
        self._async_segments_changed()

    # ---- Segment control -------------------------------------------------

    async def async_apply_segment_colors(self, updates: dict[int, RGBTuple], preset: str | None = None) -> None:
        """Send colors for some segments. Keys are 0-based segment indexes."""
        if not self.segment_count:
            raise HomeAssistantError(f"{self._model} is not a segmented model (add it to SEGMENTED_MODELS)")

        clean: dict[int, RGBTuple] = {}
        for idx, rgb in updates.items():
            if 0 <= idx < self.segment_count and rgb is not None:
                clean[idx] = tuple(max(0, min(255, int(c))) for c in rgb)  # type: ignore[assignment]
        if not clean:
            return

        # One packet per distinct color, with a bitmask of the segments using it
        groups: dict[RGBTuple, int] = {}
        for idx, rgb in clean.items():
            groups[rgb] = groups.get(rgb, 0) | (1 << idx)

        commands = [self._prepareSinglePacketData(LedCommand.POWER, [0x1])]
        for (red, green, blue), mask in groups.items():
            commands.append(self._prepareSinglePacketData(
                LedCommand.COLOR,
                [LedMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00, 0x00, 0x00, 0x00,
                 mask & 0xFF, (mask >> 8) & 0xFF]))

        await self._async_send(commands)

        for idx, rgb in clean.items():
            self.segment_colors[idx] = rgb
        self._state = True
        self._set_current_preset(preset)
        self._async_segments_changed()

    async def async_set_segment_color(self, segments: list[int], rgb_color) -> None:
        """Set one color on a list of 1-based segment numbers."""
        await self.async_apply_segment_colors({s - 1: tuple(rgb_color) for s in segments})

    async def async_set_segments(self, colors: list) -> None:
        """Set colors for segments 1..N in order. None entries are left as-is."""
        await self.async_apply_segment_colors(
            {i: tuple(c) for i, c in enumerate(colors[:self.segment_count]) if c is not None})

    async def async_apply_preset(self, name: str) -> None:
        presets = self.hass.data[DATA_PRESETS].presets
        if name not in presets:
            raise HomeAssistantError(f"No preset named '{name}'")
        colors = resample(presets[name], self.segment_count)
        await self.async_apply_segment_colors({i: tuple(c) for i, c in enumerate(colors)}, preset=name)

    def _set_current_preset(self, name: str | None) -> None:
        preset_select = self.hass.data.get(DATA_PRESET_SELECTS, {}).get(self._entry_id) if self.hass else None
        if preset_select is not None:
            preset_select.set_current(name)

    def _async_segments_changed(self) -> None:
        self.async_write_ha_state()
        for segment in self.segment_entities:
            if segment.hass is not None:
                segment.async_write_ha_state()

    # ---- Bluetooth -------------------------------------------------------

    async def _async_send(self, commands: list[bytes]) -> None:
        """Send packets over one (reused) connection, serialized per strip."""
        async with self._lock:
            for attempt in range(2):
                try:
                    client = await self._async_get_client()
                    for i, command in enumerate(commands):
                        if i:
                            await asyncio.sleep(0.02)
                        await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, command, False)
                    return
                except Exception as err:  # noqa: BLE001
                    self._client = None
                    if attempt:
                        raise HomeAssistantError(f"Failed to send to Govee light {self._mac}: {err}") from err
                    _LOGGER.debug("Send to %s failed (%s), reconnecting", self._mac, err)

    async def _async_get_client(self) -> BleakClient:
        if self._client is not None and self._client.is_connected:
            return self._client
        client = await self._connectBluetooth()
        if client is None:
            raise HomeAssistantError(f"Could not connect to Govee light {self._mac}")
        self._client = client
        return client

    async def _connectBluetooth(self) -> BleakClient | None:
        device = bluetooth.async_ble_device_from_address(self.hass, self._mac.upper(), True) or self._ble_device
        for i in range(3):
            try:
                client = await bleak_retry_connector.establish_connection(BleakClient, device, self.unique_id)
                return client
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Connect attempt %s to %s failed: %s", i + 1, self._mac, err)
                continue
        return None

    def _prepareSinglePacketData(self, cmd, payload):
        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (
                isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        cmd = cmd & 0xFF
        payload = bytes(payload)

        frame = bytes([0x33, cmd]) + bytes(payload)
        # pad frame data to 19 bytes (plus checksum)
        frame += bytes([0] * (19 - len(frame)))

        # The checksum is calculated by XORing all data bytes
        checksum = 0
        for b in frame:
            checksum ^= b

        frame += bytes([checksum & 0xFF])
        return frame


class GoveeSegmentLight(LightEntity):
    """One segment of a segmented Govee BLE strip, as its own light."""

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_should_poll = False

    def __init__(self, parent: GoveeBluetoothLight, index: int) -> None:
        self._parent = parent
        self._index = index
        self._attr_name = f"Segment {index + 1}"
        self._attr_unique_id = f"{parent.unique_id}_segment_{index + 1}"
        self._attr_icon = "mdi:led-strip-variant"
        self._last_on_color: RGBTuple = (255, 255, 255)
        self._last_on_level = 255

    @property
    def device_info(self):
        return self._parent.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Refresh the strip's segment_entities attribute now that we have an entity_id
        if self._parent.hass is not None and self._parent.entity_id:
            self._parent.async_write_ha_state()

    def _raw(self) -> RGBTuple | None:
        return self._parent.segment_colors[self._index]

    @staticmethod
    def _normalize(rgb: RGBTuple) -> RGBTuple:
        peak = max(rgb)
        return tuple(round(c * 255 / peak) for c in rgb)  # type: ignore[return-value]

    @property
    def is_on(self) -> bool | None:
        if self._parent.is_on is False:
            return False
        raw = self._raw()
        if raw is None:
            return None
        return any(raw)

    @property
    def brightness(self) -> int | None:
        raw = self._raw()
        return max(raw) if raw and any(raw) else None

    @property
    def rgb_color(self):
        raw = self._raw()
        return self._normalize(raw) if raw and any(raw) else None

    async def async_turn_on(self, **kwargs) -> None:
        raw = self._raw()
        lit = raw is not None and any(raw)
        if ATTR_RGB_COLOR in kwargs:
            base = tuple(kwargs[ATTR_RGB_COLOR])
        elif lit:
            base = self._normalize(raw)
        else:
            base = self._last_on_color
        if not any(base):
            base = self._last_on_color
        level = kwargs.get(ATTR_BRIGHTNESS, max(raw) if lit else self._last_on_level)
        color = tuple(round(c * level / 255) for c in base)
        self._last_on_color = base  # type: ignore[assignment]
        self._last_on_level = level
        await self._parent.async_apply_segment_colors({self._index: color})

    async def async_turn_off(self, **kwargs) -> None:
        raw = self._raw()
        if raw is not None and any(raw):
            self._last_on_color = self._normalize(raw)
            self._last_on_level = max(raw)
        await self._parent.async_apply_segment_colors({self._index: (0, 0, 0)})
