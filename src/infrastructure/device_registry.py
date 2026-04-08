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
    hostapi: Optional[str] = None
    default_samplerate: Optional[float] = None

    @property
    def is_input(self) -> bool:
        return self.max_input_channels > 0

    @property
    def is_output(self) -> bool:
        return self.max_output_channels > 0

    @property
    def signature(self) -> str:
        hostapi = " ".join(str(self.hostapi or "").lower().split())
        name = " ".join(str(self.name or "").lower().split())
        return (
            f"{hostapi}|{name}|"
            f"in:{int(self.max_input_channels)}|out:{int(self.max_output_channels)}"
        )


@dataclass(frozen=True)
class DeviceSnapshot:
    input_devices: List[DeviceInfo]
    output_devices: List[DeviceInfo]

    def find_input(self, index: int) -> Optional[DeviceInfo]:
        return _find_by_index(self.input_devices, index)

    def find_output(self, index: int) -> Optional[DeviceInfo]:
        return _find_by_index(self.output_devices, index)

    def find_input_signature(self, signature: str) -> Optional[DeviceInfo]:
        return _find_by_signature(self.input_devices, signature)

    def find_output_signature(self, signature: str) -> Optional[DeviceInfo]:
        return _find_by_signature(self.output_devices, signature)


def _find_by_index(devices: Iterable[DeviceInfo], index: int) -> Optional[DeviceInfo]:
    for dev in devices:
        if dev.index == index:
            return dev
    return None


def _find_by_signature(
    devices: Iterable[DeviceInfo], signature: Optional[str]
) -> Optional[DeviceInfo]:
    normalized = " ".join(str(signature or "").lower().split())
    if not normalized:
        return None
    for dev in devices:
        if dev.signature == normalized:
            return dev
    return None


def _extract_default_device_index(defaults, position: int) -> Optional[int]:
    if defaults is None:
        return None
    candidate = defaults
    try:
        if not isinstance(defaults, (str, bytes)) and len(defaults) > position:
            candidate = defaults[position]
    except TypeError:
        candidate = defaults
    try:
        return int(candidate)
    except (TypeError, ValueError):
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
        hostapis = self._hostapi_names()
        inputs: List[DeviceInfo] = []
        outputs: List[DeviceInfo] = []
        for idx, raw in enumerate(devices):
            hostapi_name = hostapis.get(raw.get("hostapi"))
            info = DeviceInfo(
                index=idx,
                name=str(raw.get("name", f"Device {idx}")),
                max_input_channels=int(raw.get("max_input_channels", 0)),
                max_output_channels=int(raw.get("max_output_channels", 0)),
                hostapi=hostapi_name,
                default_samplerate=(
                    float(raw.get("default_samplerate"))
                    if raw.get("default_samplerate")
                    else None
                ),
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

    def input_signature(self, index: Optional[int]) -> Optional[str]:
        if index is None:
            return None
        info = self.snapshot().find_input(index)
        return info.signature if info is not None else None

    def output_signature(self, index: Optional[int]) -> Optional[str]:
        if index is None:
            return None
        info = self.snapshot().find_output(index)
        return info.signature if info is not None else None

    def resolve_input_signature(self, signature: Optional[str]) -> Optional[int]:
        info = self.snapshot().find_input_signature(signature or "")
        return info.index if info is not None else None

    def resolve_output_signature(self, signature: Optional[str]) -> Optional[int]:
        info = self.snapshot().find_output_signature(signature or "")
        return info.index if info is not None else None

    def default_input_index(self) -> Optional[int]:
        snap = self.snapshot()
        defaults = getattr(getattr(self._sd, "default", None), "device", None)
        preferred = _extract_default_device_index(defaults, 0)
        if preferred is not None and snap.find_input(preferred) is not None:
            return preferred
        return snap.input_devices[0].index if snap.input_devices else None

    def default_output_index(self) -> Optional[int]:
        snap = self.snapshot()
        defaults = getattr(getattr(self._sd, "default", None), "device", None)
        preferred = _extract_default_device_index(defaults, 1)
        if preferred is not None and snap.find_output(preferred) is not None:
            return preferred
        return snap.output_devices[0].index if snap.output_devices else None

    def _hostapi_names(self) -> dict:
        query_hostapis = getattr(self._sd, "query_hostapis", None)
        if not callable(query_hostapis):
            return {}
        try:
            hostapis = query_hostapis()
        except Exception:
            return {}
        mapping = {}
        for idx, raw in enumerate(hostapis):
            try:
                mapping[idx] = str(raw.get("name", f"HostAPI {idx}"))
            except AttributeError:
                mapping[idx] = str(raw)
        return mapping
