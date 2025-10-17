import asyncio
import json
from contextlib import suppress
from typing import Callable, Optional
from urllib.parse import urlsplit

import numpy as np
import websockets
from websockets import WebSocketClientProtocol

from config.audio_config import AUDIO_WS_URL
from dto.audio_message import AudioMessage, np_to_base64


class AudioWebSocketClient:
    def __init__(
        self,
        url: Optional[str] = None,
        on_receive: Optional[Callable[[AudioMessage], None]] = None,
        mode: Optional[str] = None,  # 'json' or 'binary' (auto if None)
    ):
        self.url = url or AUDIO_WS_URL
        self._ws: Optional[WebSocketClientProtocol] = None
        self._on_receive = on_receive
        self._on_receive_binary: Optional[Callable[[bytes], None]] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._closing = False
        self._mode = mode or self._infer_mode_from_url(self.url)
        # Binary protocol state
        self._binary_started: bool = False
        self._binary_chunks_sent: int = 0
        self._binary_expected_sampwidth: int = 2  # bytes per sample (default 16-bit)
        self._binary_channels: int = 1
        self._binary_chunk_bytes: int = 0
        self._closed_by_server = False
        self._close_code: Optional[int] = None
        self._close_reason: Optional[str] = None

    @staticmethod
    def _infer_mode_from_url(url: str) -> str:
        try:
            path = urlsplit(url).path or ""
        except Exception:
            path = ""
        # Heuristic: main service uses '/ws/audio'
        if path.endswith("/ws/audio"):
            return "binary"
        return "json"

    async def connect(self):
        self._closing = False
        self._closed_by_server = False
        self._close_code = None
        self._close_reason = None
        self._ready_event.clear()
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
            with suppress(asyncio.CancelledError):
                await self._recv_task
            self._recv_task = None
        if self._ws is not None:
            close_coro = getattr(self._ws, "close", None)
            if callable(close_coro):
                await close_coro()
        self._ws = None
        self._connected.clear()
        self._ready_event.clear()
        self._reset_binary_state()

    async def prepare_stream(
        self,
        session_id: str,
        sample_rate: int,
        chunk_frames: int,
        channels: int,
        sampwidth: int = 2,
    ) -> None:
        """Prepare server-side stream if using binary mode.

        Sends an initial JSON text frame with format description.
        In JSON mode, this is a no-op.
        """
        await self._connected.wait()
        if self._mode != "binary":
            return
        if self._binary_started:
            return
        channels = int(channels) or 1
        sampwidth = int(sampwidth) or 1
        chunk_frames = int(chunk_frames) or 1
        chunk_bytes = chunk_frames * channels * sampwidth
        self._ready_event.clear()
        self._binary_started = True
        self._binary_chunks_sent = 0
        self._binary_expected_sampwidth = sampwidth
        self._binary_channels = channels
        self._binary_chunk_bytes = chunk_bytes
        start_msg = {
            "type": "start",
            "session_id": session_id,
            "sample_rate": int(sample_rate),
            "chunk_size": int(chunk_bytes),
            "channels": channels,
            "sampwidth": sampwidth,
        }
        await self._ws.send(json.dumps(start_msg))
        await self._wait_for_ready()

    async def send_audio_chunk(self, session_id: str, frame: np.ndarray):
        await self._connected.wait()
        if self._mode == "binary":
            if not self._binary_started:
                channels = 1 if frame.ndim == 1 else frame.shape[1]
                chunk_frames = int(frame.shape[0])
                await self.prepare_stream(
                    session_id,
                    sample_rate=16000,  # default if not provided explicitly
                    chunk_frames=chunk_frames,
                    channels=channels,
                    sampwidth=self._binary_expected_sampwidth or 2,
                )
            if not self._ready_event.is_set():
                await self._wait_for_ready()
            payload = self._encode_pcm(frame)
            if not payload:
                return
            await self._ws.send(payload)
            self._binary_chunks_sent += 1
            return

        # JSON mode (legacy/test server)
        msg = AudioMessage(
            type="audio_chunk",
            session_id=session_id,
            data_b64=np_to_base64(frame),
            dtype=str(frame.dtype),
            shape=frame.shape,
        )
        await self._ws.send(msg.to_json())

    async def send_control(self, session_id: str, control_type: str, **extra):
        await self._connected.wait()
        if self._mode == "binary" and control_type == "end_session":
            end_msg = {
                "type": "end_of_chunks",
                "session_id": session_id,
                "chunks_sent": int(self._binary_chunks_sent),
            }
            await self._ws.send(json.dumps(end_msg))
            self._reset_binary_state()
            return

        target_type = control_type
        if control_type == "end_session":
            target_type = "end_of_chunks"
        msg = AudioMessage(
            type=target_type,
            session_id=session_id,
            extra=extra or None,
        )
        await self._ws.send(msg.to_json())

    async def send_playback(self, session_id: str, mode: str = "background"):
        await self.send_control(session_id, "playback", mode=mode)

    async def send_playback_stop(self, session_id: str):
        await self.send_control(session_id, "playback_stop")

    async def _receiver_loop(self):
        try:
            async for msg_raw in self._ws:
                if isinstance(msg_raw, (bytes, bytearray)):
                    if self._on_receive_binary:
                        result = self._on_receive_binary(bytes(msg_raw))
                        if asyncio.iscoroutine(result):
                            try:
                                await result
                            except Exception as exc:
                                print(f"[WS] Binary handler error: {exc}")
                    continue

                if isinstance(msg_raw, str) and self._handle_text_signal(msg_raw):
                    continue

                try:
                    msg = AudioMessage.from_json(msg_raw)
                except Exception:
                    continue

                if msg.type == "ready":
                    self._ready_event.set()
                    continue
                if msg.type and msg.type.startswith("ack"):
                    self._log_ack(msg.type)
                    continue

                if self._on_receive:
                    try:
                        await self._on_receive(msg)
                    except Exception as exc:
                        print(f"[WS] Message handler error: {exc}")
        except websockets.ConnectionClosed as exc:
            if not self._closing:
                self._closed_by_server = True
                self._close_code = getattr(exc, "code", None)
                self._close_reason = getattr(exc, "reason", None)
                print(
                    f"[WS] Connection closed by server: code={self._close_code} reason={self._close_reason}"
                )
        except Exception as e:
            if not self._closing:
                print(f"[WS] Receiver loop error: {e}")
        finally:
            self._connected.clear()
            self._ready_event.clear()
            self._reset_binary_state()

    def _reset_binary_state(self) -> None:
        self._binary_started = False
        self._binary_chunks_sent = 0
        self._binary_chunk_bytes = 0

    async def _wait_for_ready(self, timeout: float = 5.0) -> None:
        if self._ready_event.is_set():
            return
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            print("[WS] Timed out waiting for server ready signal")

    def _handle_text_signal(self, text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped:
            return False
        if stripped.lower() == "ready":
            self._ready_event.set()
            return True
        if stripped.lower().startswith("ack:"):
            self._log_ack(stripped)
            return True
        return False

    def _log_ack(self, payload: str) -> None:
        try:
            count = int(payload.split(":", 1)[1])
        except (IndexError, ValueError):
            return
        print(f"[WS] Ack {count}")

    def _encode_pcm(self, frame: np.ndarray) -> bytes:
        arr = np.asarray(frame, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        channels = self._binary_channels or (arr.shape[1] if arr.ndim > 1 else 1)
        if arr.shape[1] != channels:
            if arr.shape[1] == 1 and channels > 1:
                arr = np.repeat(arr, channels, axis=1)
            else:
                arr = arr[:, :channels]
        arr = np.clip(arr, -1.0, 1.0)
        sampwidth = max(1, self._binary_expected_sampwidth)
        if sampwidth == 1:
            # Unsigned 8-bit PCM
            payload = ((arr * 127.0) + 128.0).clip(0.0, 255.0).astype(np.uint8)
        elif sampwidth == 2:
            payload = (arr * 32767.0).round().astype("<i2")
        elif sampwidth == 4:
            payload = (arr * 2147483647.0).round().astype("<i4")
        else:
            print(f"[WS] Unsupported sampwidth {sampwidth}; skipping frame")
            return b""
        raw = payload.tobytes()
        frame_size = channels * sampwidth
        if frame_size and (len(raw) % frame_size) != 0:
            # Pad to next full frame to satisfy server framing
            pad = frame_size - (len(raw) % frame_size)
            raw += b"\x00" * pad
        return raw

    @property
    def closed_by_server(self) -> bool:
        return self._closed_by_server

    @property
    def close_code(self) -> Optional[int]:
        return self._close_code

    @property
    def close_reason(self) -> Optional[str]:
        return self._close_reason
