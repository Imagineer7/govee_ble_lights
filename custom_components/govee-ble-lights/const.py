from homeassistant.helpers.device_registry import DeviceInfo

DOMAIN = "govee-ble-lights"
CONF_TYPE_API = 'API'
CONF_TYPE_BLE = 'BLE'

# HA rejects action names containing '-', so our custom actions live under
# an underscore domain: govee_ble_lights.set_segment_color, etc.
SERVICE_DOMAIN = "govee_ble_lights"

# Models whose color is set through the segment command (33 05 15 01 ...).
SEGMENTED_MODELS = ['H6053', 'H6072', 'H6102', 'H6199', 'H617F', 'H617C']

# Segment count per model. The segment bitmask is 2 bytes, so max 16.
DEFAULT_SEGMENT_COUNT = 15
SEGMENT_COUNTS: dict[str, int] = {}

# hass.data keys (kept out of hass.data[DOMAIN], which is keyed by entry_id)
DATA_BLE_LIGHTS = "govee_ble_lights_entities"      # entry_id -> GoveeBluetoothLight
DATA_PRESET_SELECTS = "govee_ble_lights_selects"   # entry_id -> GoveePresetSelect
DATA_PRESETS = "govee_ble_lights_presets"          # PresetStore

# Dashboard card served by this integration
CARD_VERSION = "1.0.0"
CARD_URL = "/govee_ble_lights/govee-segment-card.js"


def segment_count_for(model: str) -> int:
    if model not in SEGMENTED_MODELS:
        return 0
    return min(SEGMENT_COUNTS.get(model, DEFAULT_SEGMENT_COUNT), 16)


def build_device_info(mac_id: str, name: str, model: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, mac_id)},
        name=name,
        manufacturer="Govee",
        model=model,
    )
