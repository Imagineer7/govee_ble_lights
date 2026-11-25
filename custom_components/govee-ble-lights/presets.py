"""Segment color presets, stored by Home Assistant in .storage/govee_ble_lights.presets."""
from __future__ import annotations

import colorsys
from collections.abc import Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DEFAULT_SEGMENT_COUNT

STORAGE_KEY = "govee_ble_lights.presets"
STORAGE_VERSION = 1

RGB = list[int]


def _hsv(h: float, s: float = 1.0, v: float = 1.0) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return [round(r * 255), round(g * 255), round(b * 255)]


def _gradient(stops: list[RGB], n: int) -> list[RGB]:
    out = []
    for i in range(n):
        pos = i * (len(stops) - 1) / max(n - 1, 1)
        a = int(pos)
        b = min(a + 1, len(stops) - 1)
        t = pos - a
        out.append([round(stops[a][c] + (stops[b][c] - stops[a][c]) * t) for c in range(3)])
    return out


def default_presets(n: int = DEFAULT_SEGMENT_COUNT) -> dict[str, list[RGB]]:
    red, green, white = [255, 0, 0], [0, 255, 0], [255, 255, 255]
    return {
        "Rainbow": [_hsv(i / n) for i in range(n)],
        "Northern Lights": _gradient(
            [[0, 255, 110], [0, 190, 200], [120, 0, 255], [0, 190, 200], [0, 255, 110]], n),
        "Christmas": [red if i % 2 == 0 else green for i in range(n)],
        "Candy Cane": [red if i % 2 == 0 else white for i in range(n)],
        "Warm White": [[255, 160, 70] for _ in range(n)],
    }


def resample(colors: list[RGB], n: int) -> list[RGB]:
    """Stretch/shrink a preset to n segments."""
    if not colors:
        return [[0, 0, 0]] * n
    if len(colors) == n:
        return colors
    return [colors[min(int(i * len(colors) / n), len(colors) - 1)] for i in range(n)]


class PresetStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.presets: dict[str, list[RGB]] = {}
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data is None:
            self.presets = default_presets()
            await self._store.async_save({"presets": self.presets})
        else:
            self.presets = dict(data.get("presets", {}))

    def async_add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return remove

    async def _async_commit(self) -> None:
        await self._store.async_save({"presets": self.presets})
        for callback in list(self._listeners):
            callback()

    async def async_save_preset(self, name: str, colors: list[RGB]) -> None:
        clean = [[max(0, min(255, int(c))) for c in rgb[:3]] for rgb in colors]
        self.presets = {**self.presets, name: clean}
        await self._async_commit()

    async def async_delete_preset(self, name: str) -> bool:
        if name not in self.presets:
            return False
        self.presets = {k: v for k, v in self.presets.items() if k != name}
        await self._async_commit()
        return True
