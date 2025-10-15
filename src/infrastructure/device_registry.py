from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterable, List, Optional

import sounddevice as sd


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int

    @property
    def is_input(self) -> bool:
        return self.max_input_channels > 0

    @property
    def is_output(self) -> bool:
        return self.max_output_channels > 0


@dataclass(frozen=True)
class DeviceSnapshot:
    input_devices: List[DeviceInfo]
    output_devices: List[DeviceInfo]

    def find_input(self, index: int) -> Optional[DeviceInfo]:
        return _find_by_index(self.input_devices, index)

    def find_output(self, index: int) -> Optional[DeviceInfo]:
        return _find_by_index(self.output_devices, index)


def _find_by_index(devices: Iterable[DeviceInfo], index: int) -> Optional[DeviceInfo]:
    for dev in devices:
        if dev.index == index:
            return dev
    return None


class DeviceRegistry:
    """Caches sounddevice device metadata for input/output selection."""

    def __init__(self, sounddevice_module=sd):
        self._sd = sounddevice_module
        self._lock = threading.Lock()
        self._snapshot = DeviceSnapshot([], [])
        self.refresh()

    def refresh(self) -> DeviceSnapshot:
        devices = self._sd.query_devices()
        inputs: List[DeviceInfo] = []
        outputs: List[DeviceInfo] = []
        for idx, raw in enumerate(devices):
            info = DeviceInfo(
                index=idx,
                name=str(raw.get("name", f"Device {idx}")),
                max_input_channels=int(raw.get("max_input_channels", 0)),
                max_output_channels=int(raw.get("max_output_channels", 0)),
            )
            if info.is_input:
                inputs.append(info)
            if info.is_output:
                outputs.append(info)
        snapshot = DeviceSnapshot(inputs, outputs)
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def snapshot(self) -> DeviceSnapshot:
        with self._lock:
            return self._snapshot

    def is_valid_input(self, index: int) -> bool:
        return self.snapshot().find_input(index) is not None

    def is_valid_output(self, index: int) -> bool:
        return self.snapshot().find_output(index) is not None

    def default_input_index(self) -> Optional[int]:
        snap = self.snapshot()
        return snap.input_devices[0].index if snap.input_devices else None

    def default_output_index(self) -> Optional[int]:
        snap = self.snapshot()
        return snap.output_devices[0].index if snap.output_devices else None
