"""Preset picker for segmented Govee BLE strips."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MODEL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (DOMAIN, DATA_BLE_LIGHTS, DATA_PRESET_SELECTS, DATA_PRESETS,
                    segment_count_for, build_device_info)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    hub = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if hub is None or hub.address is None:
        return
    model = config_entry.data.get(CONF_MODEL)
    if not segment_count_for(model):
        return
    async_add_entities([GoveePresetSelect(hub.address, config_entry, model)])


class GoveePresetSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Preset"
    _attr_icon = "mdi:palette"
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"presets"})

    def __init__(self, address: str, config_entry: ConfigEntry, model: str) -> None:
        mac_id = address.replace(":", "")
        self._entry_id = config_entry.entry_id
        self._attr_unique_id = f"{mac_id}_preset"
        self._attr_device_info = build_device_info(mac_id, config_entry.title or f"Govee {model}", model)
        self._attr_current_option = None

    @property
    def _store(self):
        return self.hass.data[DATA_PRESETS]

    @property
    def options(self) -> list[str]:
        return list(self._store.presets)

    @property
    def extra_state_attributes(self):
        return {"presets": self._store.presets}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.data.setdefault(DATA_PRESET_SELECTS, {})[self._entry_id] = self
        self.async_on_remove(self._store.async_add_listener(self._handle_presets_changed))
        # Let the light publish our entity_id (preset_entity attribute) for the card
        light = self.hass.data.get(DATA_BLE_LIGHTS, {}).get(self._entry_id)
        if light is not None and light.hass is not None:
            light.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        selects = self.hass.data.get(DATA_PRESET_SELECTS, {})
        if selects.get(self._entry_id) is self:
            selects.pop(self._entry_id)

    @callback
    def _handle_presets_changed(self) -> None:
        if self._attr_current_option not in self._store.presets:
            self._attr_current_option = None
        self.async_write_ha_state()

    @callback
    def set_current(self, name: str | None) -> None:
        self._attr_current_option = name if name in self._store.presets else None
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        light = self.hass.data.get(DATA_BLE_LIGHTS, {}).get(self._entry_id)
        if light is None:
            raise HomeAssistantError("The light for this preset selector isn't loaded")
        await light.async_apply_preset(option)
