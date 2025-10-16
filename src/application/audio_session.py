import asyncio
import uuid
from typing import Optional

from dto.audio_message import base64_to_np
from exceptions.audio_exceptions import AudioError
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

    async def run_once(
        self,
        timeout: Optional[float] = None,
        playback_timeout: Optional[float] = None,
    ):
        print(f"[Session {self.session_id}] Started")
        await self.ws.connect()
        self.spk.spk.start_output()
        try:
            self.mic.mic.start_recording()
        except Exception:
            self.spk.spk.stop_output()
            raise

        sender = asyncio.create_task(self._send_loop())
        run_error: Optional[Exception] = None
        try:
            try:
                if timeout is not None:
                    await asyncio.wait_for(self._recording_done(), timeout)
                else:
                    # Wait indefinitely until recording is stopped by user
                    await self._recording_done()
            except asyncio.TimeoutError:
                # Timed recording window elapsed; proceed to stop
                pass
        except Exception as exc:  # pragma: no cover - unexpected flow
            run_error = exc
        finally:
            self.mic.mic.stop_recording()

        try:
            if playback_timeout is not None:
                await asyncio.wait_for(sender, timeout=playback_timeout)
            else:
                await sender
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            sender.cancel()
            if run_error is None:
                run_error = AudioError("Sender task timed out")
                run_error.__cause__ = exc
        except Exception as exc:
            if run_error is None:
                run_error = exc

        # Notify the server that input has ended and we are ready for the response
        try:
            await self.ws.send_control(self.session_id, "end_session")
        except Exception as exc:
            if run_error is None:
                run_error = exc

        try:
            if run_error is None:
                while True:
                    if playback_timeout is not None:
                        data = await asyncio.wait_for(
                            self._queue.get(), timeout=playback_timeout
                        )
                    else:
                        data = await self._queue.get()
                    if data is None:
                        break
                    await self.spk.play(data)
                print(f"[Session {self.session_id}] Completed")
        finally:
            self.spk.spk.stop_output()

        if run_error is not None:
            raise run_error

    async def _send_loop(self):
        # Stream while recording is active
        while self.mic.mic.is_recording:
            samples = await self.mic.get_samples()
            if samples is not None:
                await self.ws.send_audio_chunk(self.session_id, samples)
        # After recording stops, drain any remaining buffered frames briefly
        idle_after_stop = 0
        max_idle_iters = 100  # ~1s given MicrophoneAsyncAdapter sleep of 10ms
        while idle_after_stop < max_idle_iters:
            samples = await self.mic.get_samples()
            if samples is None:
                idle_after_stop += 1
                continue
            idle_after_stop = 0
            await self.ws.send_audio_chunk(self.session_id, samples)
        print(f"[Session {self.session_id}] Sender finished")

    async def _recording_done(self):
        while self.mic.mic.is_recording:
            await asyncio.sleep(0.05)
