import asyncio

from application.audio_session import AudioSession
from infrastructure.microphone_interface import MicrophoneInterface
from infrastructure.sounds_adapters import MicrophoneAsyncAdapter, SpeakerAsyncAdapter
from infrastructure.speaker_interface import SpeakerInterface
from infrastructure.websocket_client import AudioWebSocketClient


async def main():
    mic = MicrophoneInterface(samplerate=16000, channels=1, blocksize=1024)
    spk = SpeakerInterface(samplerate=16000, channels=1, blocksize=1024)

    mic_adapter = MicrophoneAsyncAdapter(mic)
    spk_adapter = SpeakerAsyncAdapter(spk)
    ws_client = AudioWebSocketClient()

    try:
        session = AudioSession(ws_client, mic_adapter, spk_adapter)
        await session.run_once(timeout=5, playback_timeout=30.0)
    finally:
        await ws_client.close(reason="main_shutdown", trigger="main")
        spk_adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
