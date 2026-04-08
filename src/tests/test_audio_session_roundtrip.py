import asyncio
import threading
import uuid
from pathlib import Path
from typing import Awaitable

import numpy as np
import pytest
import pytest_asyncio
import websockets

import config.audio_config as audio_config
from application.audio_session import AudioSession
from infrastructure.sounds_adapters import MicrophoneAsyncAdapter, SpeakerAsyncAdapter
from infrastructure.websocket_client import AudioWebSocketClient
from tests.websocket_server import (
    OUTPUT_DIR,
    handle_client,
    playback_acks,
    sessions,
    start_params,
)


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


async def unlink_with_retry(path: Path, attempts: int = 10, delay: float = 0.05) -> bool:
    for _ in range(attempts):
        if not path.exists():
            return True
        try:
            path.unlink()
            return True
        except PermissionError:
            await asyncio.sleep(delay)
    return not path.exists()


@pytest_asyncio.fixture
async def audio_test_server(monkeypatch, mode):
    # Switch between legacy JSON endpoint and binary '/ws/audio'
    if mode == "binary":
        url = "ws://127.0.0.1:8765/ws/audio"
    else:
        url = "ws://127.0.0.1:8765"
    monkeypatch.setenv("AUDIO_WS_URL", url)
    monkeypatch.setattr(audio_config, "AUDIO_WS_URL", url)
    session_id = f"test-session-{mode}-{uuid.uuid4().hex[:8]}"
    target = Path(OUTPUT_DIR) / f"{session_id}.wav"
    if target.exists():
        await unlink_with_retry(target)

    try:
        server = await websockets.serve(handle_client, "127.0.0.1", 8765, max_size=None)
    except OSError as exc:  # pragma: no cover - sandboxed CI without socket perms
        pytest.skip(f"Unable to start local websocket test server: {exc}")
    try:
        yield target, url, session_id
    finally:
        server.close()
        await server.wait_closed()
        sessions.clear()
        start_params.clear()
        playback_acks.clear()
        if target.exists():
            await unlink_with_retry(target)


async def run_with_timeout(coro: Awaitable, seconds: float, stage: str):
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        pytest.fail(f"Stage '{stage}' timed out after {seconds}s")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["json", "binary"])  # exercise both protocols
async def test_audio_session_roundtrip(audio_test_server, mode):
    target, url, session_id = audio_test_server
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
    ws_client = AudioWebSocketClient(url=url)
    session = AudioSession(ws_client, mic_adapter, spk_adapter)
    session.session_id = session_id

    await run_with_timeout(session.run_once(timeout=0.2), 5.0, "session run")
    await run_with_timeout(
        ws_client.close(reason="test_teardown", trigger="pytest.teardown"),
        2.0,
        "websocket close",
    )
    assert ws_client.closed_by_server is False

    if mode == "binary":
        ack_list = playback_acks.get(session_id)
        assert ack_list, f"Expected playback acknowledgment for session '{session_id}'"
        assert any(
            ack["type"] == "playback_ack"
            and ack["message_id"] == "utt-1"
            and ack["status"] == "played"
            for ack in ack_list
        )

    assert spk.frames, "Speaker did not receive any frames"
    combined = np.concatenate(spk.frames, axis=0)
    assert combined.ndim == 2
    assert combined.shape[1] == 1

    assert target.exists(), "Websocket server did not persist WAV output"
    if mode == "binary":
        params = start_params.get(session_id)
        assert params is not None
        expected_chunk_bytes = blocksize * params["channels"] * params["sampwidth"]
        assert params["chunk_size"] == blocksize
        assert params["chunk_size_bytes"] == expected_chunk_bytes
