import asyncio
import os
import threading
from pathlib import Path
from typing import Awaitable

import numpy as np
import pytest
import pytest_asyncio
import websockets

from application.audio_session import AudioSession
from infrastructure.sounds_adapters import MicrophoneAsyncAdapter, SpeakerAsyncAdapter
from infrastructure.websocket_client import AudioWebSocketClient
from tests.websocket_server import OUTPUT_DIR, handle_client, sessions

os.environ.setdefault("AUDIO_WS_URL", "ws://127.0.0.1:8765")


class DummyMicrophoneInterface:
    def __init__(self, frames):
        self._frames = list(frames)
        self._is_recording = False

    def start_recording(self):
        self._is_recording = True

    def stop_recording(self):
        self._is_recording = False

    def get_samples(self, blocking=False, timeout=None):
        if self._frames:
            return self._frames.pop(0)
        return None

    @property
    def is_recording(self):
        return self._is_recording


class DummySpeakerInterface:
    def __init__(self):
        self.frames = []
        self._is_playing = False
        self._lock = threading.Lock()

    def start_output(self):
        self._is_playing = True

    def stop_output(self):
        self._is_playing = False

    @property
    def is_playing(self):
        return self._is_playing

    def play(self, samples):
        if not self._is_playing:
            raise RuntimeError("Speaker not started")
        with self._lock:
            self.frames.append(np.asarray(samples))


@pytest_asyncio.fixture
async def audio_test_server(monkeypatch):
    url = "ws://127.0.0.1:8765"
    monkeypatch.setenv("AUDIO_WS_URL", url)
    target = Path(OUTPUT_DIR) / "test-session.wav"
    if target.exists():
        target.unlink()

    try:
        server = await websockets.serve(handle_client, "127.0.0.1", 8765, max_size=None)
    except OSError as exc:  # pragma: no cover - sandboxed CI without socket perms
        pytest.skip(f"Unable to start local websocket test server: {exc}")
    try:
        yield target
    finally:
        server.close()
        await server.wait_closed()
        sessions.clear()
        if target.exists():
            target.unlink()


async def run_with_timeout(coro: Awaitable, seconds: float, stage: str):
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        pytest.fail(f"Stage '{stage}' timed out after {seconds}s")


@pytest.mark.asyncio
async def test_audio_session_roundtrip(audio_test_server):
    blocksize = 1024
    samplerate = 16000
    frames = []
    t = np.arange(blocksize) / samplerate
    tone = (0.1 * np.sin(2 * np.pi * 440 * t)).astype("float32").reshape(-1, 1)
    for _ in range(3):
        frames.append(tone.copy())

    mic = DummyMicrophoneInterface(frames)
    spk = DummySpeakerInterface()
    mic_adapter = MicrophoneAsyncAdapter(mic)
    spk_adapter = SpeakerAsyncAdapter(spk)
    ws_client = AudioWebSocketClient()
    session = AudioSession(ws_client, mic_adapter, spk_adapter)
    session.session_id = "test-session"

    await run_with_timeout(session.run_once(timeout=0.2), 5.0, "session run")
    await run_with_timeout(ws_client.close(), 2.0, "websocket close")

    assert spk.frames, "Speaker did not receive any frames"
    combined = np.concatenate(spk.frames, axis=0)
    assert combined.ndim == 2
    assert combined.shape[1] == 1

    target = audio_test_server
    assert target.exists(), "Websocket server did not persist WAV output"
