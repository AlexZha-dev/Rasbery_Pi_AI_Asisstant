import io
import wave

import numpy as np
import pytest

from application.audio_session import AudioSession
from dto.audio_message import AudioMessage


class DummyWS:
    def __init__(self):
        self._on_receive = None
        self._on_receive_binary = None


@pytest.mark.asyncio
async def test_audio_session_handles_wav_playback():
    ws = DummyWS()
    session = AudioSession(ws, object(), object())
    msg_start = AudioMessage(
        type="playback_file_start",
        session_id=session.session_id,
        extra={"file": {"format": "wav"}},
    )
    await session._handle_playback_start(msg_start)

    buf = io.BytesIO()
    samplerate = 16000
    t = np.arange(1024) / samplerate
    tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    tone_i16 = (tone * 32767.0).astype("<i2")
    with wave.open(buf, "wb") as wav_writer:
        wav_writer.setnchannels(1)
        wav_writer.setsampwidth(2)
        wav_writer.setframerate(samplerate)
        wav_writer.writeframes(tone_i16.tobytes())

    payload = buf.getvalue()
    midpoint = len(payload) // 2
    await session._on_binary(payload[:midpoint])
    await session._on_binary(payload[midpoint:])

    await session._handle_playback_done()

    result = await session._queue.get()
    assert result is not None
    assert result.shape[1] == 1
    assert np.max(np.abs(result)) > 0
