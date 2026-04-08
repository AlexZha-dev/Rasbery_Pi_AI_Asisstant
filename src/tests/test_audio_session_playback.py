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


@pytest.mark.asyncio
async def test_audio_session_handles_float32_pcm_playback():
    ws = DummyWS()
    session = AudioSession(ws, object(), object())
    msg_start = AudioMessage(
        type="playback_file_start",
        session_id=session.session_id,
        extra={
            "file": {
                "format": "pcm",
                "pcm_format": "f32le",
                "sample_rate": 16000,
                "channels": 1,
                "sampwidth": 4,
            }
        },
    )
    await session._handle_playback_start(msg_start)

    tone = np.array([0.0, 0.25, -0.25, 0.5], dtype="<f4")
    await session._on_binary(tone.tobytes())

    result = await session._queue.get()
    assert result is not None
    assert result.shape == (4, 1)
    assert np.allclose(result.reshape(-1), tone.astype(np.float32), atol=1e-6)


@pytest.mark.asyncio
async def test_audio_session_infers_float32_pcm_from_format_and_frame_size():
    ws = DummyWS()
    session = AudioSession(ws, object(), object())
    msg_start = AudioMessage(
        type="playback_file_start",
        session_id=session.session_id,
        extra={
            "file": {
                "format": "f32le",
                "sample_rate": 16000,
                "channels": 1,
                "frame_size": 4,
            }
        },
    )
    await session._handle_playback_start(msg_start)

    tone = np.array([0.1, -0.2, 0.3, -0.4], dtype="<f4")
    await session._on_binary(tone.tobytes())

    result = await session._queue.get()
    assert result is not None
    assert result.shape == (4, 1)
    assert np.allclose(result.reshape(-1), tone.astype(np.float32), atol=1e-6)


@pytest.mark.asyncio
async def test_audio_session_accepts_server_assigned_session_id_for_playback():
    ws = DummyWS()
    session = AudioSession(ws, object(), object())
    local_session_id = session.session_id
    server_session_id = "server-generated-session"

    msg_start = AudioMessage(
        type="playback_file_start",
        session_id=server_session_id,
        extra={
            "file": {
                "format": "pcm",
                "pcm_format": "f32le",
                "sample_rate": 16000,
                "channels": 1,
                "sampwidth": 4,
            }
        },
    )

    await session._on_receive(msg_start)
    await session._on_binary(np.array([0.2, -0.1], dtype="<f4").tobytes())

    result = await session._queue.get()

    assert local_session_id != server_session_id
    assert session.server_session_id == server_session_id
    assert session.active_session_id == server_session_id
    assert result is not None
    assert np.allclose(result.reshape(-1), np.array([0.2, -0.1], dtype=np.float32))
