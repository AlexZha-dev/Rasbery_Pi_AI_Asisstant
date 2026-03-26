import base64
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

MAX_AUDIO_ARRAY_BYTES = 8 * 1024 * 1024


def np_to_base64(arr: np.ndarray) -> str:
    return base64.b64encode(arr.tobytes()).decode("ascii")


def base64_to_np(
    b64: str,
    dtype: str,
    shape: Tuple[int],
    *,
    max_decoded_bytes: int = MAX_AUDIO_ARRAY_BYTES,
) -> np.ndarray:
    if b64 is None:
        raise ValueError("Missing audio payload")
    if dtype is None:
        raise ValueError("Missing dtype")
    if shape is None:
        raise ValueError("Missing shape")

    np_dtype = np.dtype(dtype)
    try:
        dims = tuple(int(dim) for dim in shape)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid shape: {shape!r}") from exc

    if not dims:
        raise ValueError("Audio shape must not be empty")
    if any(dim < 0 for dim in dims):
        raise ValueError(f"Audio shape dimensions must be non-negative: {dims!r}")

    expected_items = 1
    for dim in dims:
        expected_items *= dim
    expected_bytes = expected_items * int(np_dtype.itemsize)
    if expected_bytes < 0:
        raise ValueError("Decoded payload size must not be negative")
    if expected_bytes > int(max_decoded_bytes):
        raise ValueError(
            f"Decoded payload too large: {expected_bytes} > {max_decoded_bytes}"
        )

    raw = base64.b64decode(b64, validate=True)
    if len(raw) != expected_bytes:
        raise ValueError(
            f"Decoded payload size mismatch: got {len(raw)} bytes, expected {expected_bytes}"
        )

    return np.frombuffer(raw, dtype=np_dtype).reshape(dims)


@dataclass
class AudioMessage:
    type: str  # e.g. 'audio_chunk', 'response.start'
    session_id: Optional[str]
    data_b64: Optional[str] = None
    dtype: Optional[str] = None
    shape: Optional[Tuple[int]] = None
    extra: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        data = {"type": self.type}
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.data_b64 is not None:
            data.update(
                {
                    "data_b64": self.data_b64,
                    "dtype": self.dtype,
                    "shape": list(self.shape),
                }
            )
        if self.extra:
            data.update(self.extra)
        return json.dumps(data)

    @staticmethod
    def from_json(text: str) -> "AudioMessage":
        obj = json.loads(text)
        extra_keys = {"type", "session_id", "data_b64", "dtype", "shape"}
        extra = {k: v for k, v in obj.items() if k not in extra_keys}
        return AudioMessage(
            type=obj.get("type"),
            session_id=obj.get("session_id"),
            data_b64=obj.get("data_b64"),
            dtype=obj.get("dtype"),
            shape=tuple(obj.get("shape")) if obj.get("shape") else None,
            extra=extra or None,
        )
