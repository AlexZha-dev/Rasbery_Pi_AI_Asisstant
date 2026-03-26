import base64

import numpy as np
import pytest

from dto.audio_message import base64_to_np


def test_base64_to_np_rejects_size_mismatch():
    data = base64.b64encode(b"\x00\x01").decode("ascii")
    with pytest.raises(ValueError, match="size mismatch"):
        base64_to_np(data, "int16", (2, 1))


def test_base64_to_np_rejects_oversized_payload():
    arr = np.zeros((2048, 1), dtype=np.float32)
    b64 = base64.b64encode(arr.tobytes()).decode("ascii")
    with pytest.raises(ValueError, match="too large"):
        base64_to_np(b64, "float32", arr.shape, max_decoded_bytes=128)


def test_base64_to_np_accepts_empty_payload_for_zero_length_shape():
    result = base64_to_np("", "float32", (0, 1))
    assert result.shape == (0, 1)
    assert result.dtype == np.float32
