import asyncio
from typing import Callable, Optional

import numpy as np
import websockets

from config.audio_config import AUDIO_WS_URL
from dto.audio_message import AudioMessage


class AudioWebSocketClient:
    def __init__(
        self,
        url: Optional[str] = None,
        on_receive: Optional[Callable[[AudioMessage], None]] = None,
    ):
        self.url = url or AUDIO_WS_URL
        self._ws = None
        self._on_receive = on_receive
        self._recv_task = None
        self._connected = asyncio.Event()
        self._closing = False

    async def connect(self):
        while True:
            try:
                self._ws = await websockets.connect(self.url, max_size=None)
                self._connected.set()
                self._recv_task = asyncio.create_task(self._receiver_loop())
                print(f"[WS] Connected to {self.url}")
                return
            except Exception as e:
                print(f"[WS] Connection failed: {e}. Retrying...")
                await asyncio.sleep(2)

    async def close(self):
        self._closing = True
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()

    async def send_audio_chunk(self, session_id: str, frame: np.ndarray):
        await self._connected.wait()
        from dto.audio_message import np_to_base64

        msg = AudioMessage(
            type="audio_chunk",
            session_id=session_id,
            data_b64=np_to_base64(frame),
            dtype=str(frame.dtype),
            shape=frame.shape,
        )
        await self._ws.send(msg.to_json())

    async def send_control(self, session_id: str, control_type: str):
        await self._connected.wait()
        msg = AudioMessage(type=control_type, session_id=session_id)
        await self._ws.send(msg.to_json())

    async def _receiver_loop(self):
        try:
            async for msg_raw in self._ws:
                msg = AudioMessage.from_json(msg_raw)
                if self._on_receive:
                    asyncio.create_task(self._on_receive(msg))
        except Exception as e:
            if not self._closing:
                print(f"[WS] Receiver loop error: {e}")
