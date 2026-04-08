from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from application.audio_session import AudioSession
from infrastructure.device_registry import DeviceRegistry
from infrastructure.microphone_interface import MicrophoneInterface
from infrastructure.sounds_adapters import MicrophoneAsyncAdapter, SpeakerAsyncAdapter
from infrastructure.speaker_output import SpeakerInterface
from infrastructure.websocket_client import AudioWebSocketClient


@dataclass
class ClientRuntime:
    microphone: MicrophoneInterface
    speaker: SpeakerInterface
    registry: DeviceRegistry
    microphone_adapter: MicrophoneAsyncAdapter
    speaker_adapter: SpeakerAsyncAdapter

    def create_session(
        self, *, ws_url: Optional[str] = None
    ) -> Tuple[AudioSession, AudioWebSocketClient]:
        client = AudioWebSocketClient(url=ws_url) if ws_url else AudioWebSocketClient()
        session = AudioSession(
            client,
            self.microphone_adapter,
            self.speaker_adapter,
        )
        return session, client


def create_client_runtime(
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    blocksize: int = 1024,
) -> ClientRuntime:
    microphone = MicrophoneInterface(
        samplerate=sample_rate,
        channels=channels,
        blocksize=blocksize,
    )
    speaker = SpeakerInterface(
        samplerate=sample_rate,
        channels=channels,
        blocksize=blocksize,
    )
    registry = DeviceRegistry()
    return ClientRuntime(
        microphone=microphone,
        speaker=speaker,
        registry=registry,
        microphone_adapter=MicrophoneAsyncAdapter(microphone),
        speaker_adapter=SpeakerAsyncAdapter(speaker),
    )
