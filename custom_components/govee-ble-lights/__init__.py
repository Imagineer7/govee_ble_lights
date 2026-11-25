from __future__ import annotations

import asyncio

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import (CONF_API_KEY, CONF_MODEL, MAJOR_VERSION, MINOR_VERSION)
from homeassistant.helpers.storage import Store

from .govee_api import GoveeAPI

from .const import DOMAIN, DATA_PRESETS, CARD_URL, CARD_VERSION
from .presets import PresetStore
from .services import async_register_services
from pathlib import Path
import logging

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["light", "select"]


class Hub:
    def __init__(self, api: GoveeAPI | None, address: str = None, devices: list = None) -> None:
        """Init Govee dummy hub."""
        self.api = api
        self.devices = devices
        self.address = address


async def async_setup_api(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up Govee API"""
    assert config_entry.data.get(CONF_API_KEY) is not None
    hass.async_create_task(internal_api_setup(hass, config_entry))


async def internal_api_setup(hass: HomeAssistant, entry: ConfigEntry):
    api_key = entry.data.get(CONF_API_KEY)
    api = GoveeAPI(api_key)

    devices = await api.list_devices()
    _LOGGER.debug(f"Govee devices: %s", devices)

    store = Store(hass, 1, f"{DOMAIN}/{api_key}.json")
    await store.async_save(devices)
    await internal_cache_setup(hass, api, entry, devices)


UNIQUE_DEVICES = {}


async def internal_cache_setup(
        hass: HomeAssistant, api: GoveeAPI, entry: ConfigEntry, devices: list = None
):
    if devices is None:
        store = Store(hass, 1, f"{DOMAIN}/{entry.data.get(CONF_API_KEY)}.json")
        devices = await store.async_load()
        if devices:
            _LOGGER.debug(f"{len(devices)} devices loaded from cache!")
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = Hub(api, devices=devices)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


def internal_unique_devices(uid: str, devices: list) -> list:
    """For support multiple integrations - bind each device to one integraion.
    To avoid duplicates.
    """
    return [
        device
        for device in devices
        if UNIQUE_DEVICES.setdefault(device["device"], uid) == uid
    ]


async def async_setup_ble(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee BLE"""
    address = entry.unique_id
    assert address is not None
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Govee BLE device with address {address}"
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = Hub(None, address=address)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Govee BLE device from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    if entry.data.get(CONF_API_KEY):
        await async_setup_api(hass, entry)
    if entry.data.get(CONF_MODEL):
        await async_setup_ble(hass, entry)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if (MAJOR_VERSION, MINOR_VERSION) < (2025, 7):
        raise Exception("unsupported hass version, need at least 2025.7")

    # init storage for registries
    hass.data[DOMAIN] = {}

    # Segment presets + govee_ble_lights.* actions
    presets = PresetStore(hass)
    await presets.async_load()
    hass.data[DATA_PRESETS] = presets
    async_register_services(hass)

    # Serve and auto-load the segment dashboard card
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        card_path = Path(__file__).parent / "frontend" / "govee-segment-card.js"
        await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(card_path), False)])
        add_extra_js_url(hass, f"{CARD_URL}?v={CARD_VERSION}")
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not register the Govee segment card: %s", err)

    return True
