import asyncio
import uuid
from dto.audio_message import base64_to_np
from infrastructure.websocket_client import AudioWebSocketClient

class AudioSession:
    def __init__(self, ws: AudioWebSocketClient, mic_adapter, spk_adapter):
        self.ws = ws
        self.mic = mic_adapter
        self.spk = spk_adapter
        self.session_id = str(uuid.uuid4())
        self._queue = asyncio.Queue()
        ws._on_receive = self._on_receive

    async def _on_receive(self, msg):
        if msg.session_id != self.session_id:
            return
        if msg.type == "audio_chunk":
            data = base64_to_np(msg.data_b64, msg.dtype, msg.shape)
            await self._queue.put(data)
        elif msg.type == "end_session":
            await self._queue.put(None)

    async def run_once(self, timeout: float = 10.0):
        print(f"[Session {self.session_id}] Started")
        await self.ws.connect()
        self.mic.mic.start_recording()

        sender = asyncio.create_task(self._send_loop())
        try:
            await asyncio.wait_for(self._recording_done(), timeout)
        except asyncio.TimeoutError:
            pass

        self.mic.mic.stop_recording()
        await self.ws.send_control(self.session_id, "end_session")
        await sender

        while True:
            data = await self._queue.get()
            if data is None:
                break
            await self.spk.play(data)
        print(f"[Session {self.session_id}] Completed")

    async def _send_loop(self):
        while self.mic.mic._is_recording:
            samples = await self.mic.get_samples()
            if samples is not None:
                await self.ws.send_audio_chunk(self.session_id, samples)
        print(f"[Session {self.session_id}] Sender finished")

    async def _recording_done(self):
        while self.mic.mic._is_recording:
            await asyncio.sleep(0.05)
