import base64
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np


def np_to_base64(arr: np.ndarray) -> str:
    return base64.b64encode(arr.tobytes()).decode("ascii")


def base64_to_np(b64: str, dtype: str, shape: Tuple[int]) -> np.ndarray:
    raw = base64.b64decode(b64)
    return np.frombuffer(raw, dtype=dtype).reshape(shape)


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
