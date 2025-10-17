import asyncio
import io
import uuid
import wave
from typing import Any, Dict, Optional, Tuple

import numpy as np

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
        self._playback_finished = False
        self._playback_meta: Optional[Dict[str, Any]] = None
        self._pending_playback_files: int = 0
        self._playback_session_active: bool = False  # wrapper: playback.start -> playback.end
        self._final_event_received = False
        ws._on_receive = self._on_receive
        ws._on_receive_binary = self._on_binary

    async def _on_receive(self, msg):
        if msg.session_id not in {None, self.session_id}:
            return
        if msg.type == "audio_chunk":
            data = base64_to_np(msg.data_b64, msg.dtype, msg.shape)
            await self._queue.put(data)
        elif msg.type == "playback_file_start":
            await self._handle_playback_start(msg)
        elif msg.type == "playback_file_done":
            await self._handle_playback_done()
        elif msg.type in {
            "end_session",
            "playback_done",
            "final",
            "tts_complete",
            "response.end",
        }:
            self._final_event_received = True
            await self._maybe_complete_playback()
        elif msg.type == "playback.queue_status":
            # Informational: don't finish session based on generation_done alone.
            # The server sends playback_file_start/done for actual audio, and
            # may send playback.end or response.end when the turn is complete.
            pass
        elif msg.type == "playback.start":
            self._playback_session_active = True
        elif msg.type == "playback.end":
            self._playback_session_active = False
            self._final_event_received = True
            await self._maybe_complete_playback()
        elif msg.type in {"playback_stopped", "stop_playback"}:
            self._final_event_received = True
            await self._maybe_complete_playback()

    async def run_once(
        self,
        timeout: Optional[float] = None,
        playback_timeout: Optional[float] = None,
    ):
        print(f"[Session {self.session_id}] Started")
        await self.ws.connect()
        self._playback_finished = False
        self._playback_meta = None
        self._pending_playback_files = 0
        self._final_event_received = False
        # Prepare server-side stream if binary protocol is used
        try:
            await self.ws.prepare_stream(
                self.session_id,
                sample_rate=getattr(self.mic.mic, "samplerate", 16000),
                chunk_frames=getattr(self.mic.mic, "blocksize", 1024),
                channels=getattr(self.mic.mic, "channels", 1),
                sampwidth=2,
            )
        except AttributeError:
            # Older client without prepare_stream or non-binary mode
            pass
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
            await self.ws.send_playback(self.session_id, mode="background")
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

    async def _handle_playback_start(self, msg):
        extra = msg.extra or {}
        file_field = extra.get("file")
        params = extra.get("params") if isinstance(extra.get("params"), dict) else {}
        # Merge metadata from both top-level and nested structures to be robust
        meta_srcs = []
        if isinstance(file_field, dict):
            meta_srcs.append(file_field)
        if isinstance(extra, dict):
            meta_srcs.append(extra)
        if isinstance(params, dict):
            meta_srcs.append(params)

        def _first(key, default=None):
            for src in meta_srcs:
                val = src.get(key)
                if val is not None:
                    return val
            return default

        playback_format = str(_first("format", "pcm")).lower()
        if playback_format == "wav":
            self._playback_meta = {"format": "wav", "buffer": bytearray()}
            self._pending_playback_files += 1
            return
        if playback_format != "pcm":
            # Default to PCM if unspecified or unknown to avoid dropping audio
            playback_format = "pcm"

        sample_rate = int(_first("sample_rate", 16000))
        channels = int(_first("channels", 1))
        # Accept sampwidth or bit_depth_bytes synonyms
        sampwidth = int(_first("sampwidth", _first("bit_depth_bytes", 2)))
        pcm_format = str(_first("pcm_format", "s16le")).lower()

        pcm_meta = self._build_pcm_meta(
            channels=channels,
            sampwidth=sampwidth,
            pcm_format=pcm_format,
            sample_rate=sample_rate,
        )
        if pcm_meta is None:
            print("[Session] Unsupported PCM format; skipping playback")
            self._playback_meta = None
            return
        self._playback_meta = pcm_meta
        self._pending_playback_files += 1

    async def _handle_playback_done(self):
        meta = self._playback_meta
        if not meta:
            return
        if meta.get("format") == "wav":
            await self._flush_wav_buffer(meta.get("buffer", bytearray()))
        self._playback_meta = None
        if self._pending_playback_files > 0:
            self._pending_playback_files -= 1
        await self._maybe_complete_playback()

    async def _on_binary(self, payload: bytes):
        meta = self._playback_meta
        if not meta or not payload:
            return
        fmt = meta.get("format", "pcm")
        if fmt == "wav":
            meta.setdefault("buffer", bytearray()).extend(payload)
            return
        arr = self._decode_pcm_bytes(payload, meta)
        if arr is not None:
            await self._queue.put(arr)

    def _build_pcm_meta(
        self,
        *,
        channels: int,
        sampwidth: int,
        pcm_format: str,
        sample_rate: int,
    ) -> Optional[Dict[str, Any]]:
        signed, endian = self._parse_pcm_format(pcm_format, sampwidth)
        dtype = self._resolve_pcm_dtype(sampwidth, signed, endian)
        if dtype is None:
            return None
        scale, offset = self._pcm_scale_offset(sampwidth, signed)
        return {
            "format": "pcm",
            "dtype": dtype,
            "scale": scale,
            "offset": offset,
            "channels": max(1, int(channels)),
            "sample_rate": int(sample_rate),
            "sampwidth": int(sampwidth),
            "signed": signed,
        }

    async def _flush_wav_buffer(self, buffer: bytearray) -> None:
        if not buffer:
            return
        try:
            with wave.open(io.BytesIO(bytes(buffer)), "rb") as wav_reader:
                channels = wav_reader.getnchannels()
                sampwidth = wav_reader.getsampwidth()
                sample_rate = wav_reader.getframerate()
                frame_count = wav_reader.getnframes()
                raw = wav_reader.readframes(frame_count)
        except wave.Error as exc:
            print(f"[Session] Failed to decode WAV playback: {exc}")
            return
        fmt = "u8" if sampwidth == 1 else f"s{8 * sampwidth}le"
        pcm_meta = self._build_pcm_meta(
            channels=channels,
            sampwidth=sampwidth,
            pcm_format=fmt,
            sample_rate=sample_rate,
        )
        if pcm_meta is None:
            print("[Session] Unsupported WAV PCM parameters; skipping playback")
            return
        arr = self._decode_pcm_bytes(raw, pcm_meta)
        if arr is not None:
            await self._queue.put(arr)

    def _decode_pcm_bytes(
        self, payload: bytes, meta: Dict[str, Any]
    ) -> Optional[np.ndarray]:
        dtype = meta.get("dtype")
        channels = int(meta.get("channels", 1))
        if dtype is None:
            return None
        try:
            arr = np.frombuffer(payload, dtype=dtype)
        except ValueError:
            return None
        if channels > 1:
            frame_count = arr.size // channels
            if frame_count == 0:
                    return None
            arr = arr[: frame_count * channels].reshape(frame_count, channels)
        else:
            arr = arr.reshape(-1, 1)
        arr = arr.astype(np.float32)
        offset = float(meta.get("offset", 0.0))
        if offset:
            arr = arr - offset
        scale = float(meta.get("scale") or 1.0)
        if scale:
            arr = arr / scale
        return arr

    async def _maybe_complete_playback(self) -> None:
        if self._playback_finished:
            return
        if self._pending_playback_files > 0:
            return
        if self._playback_meta is not None:
            return
        if not self._final_event_received:
            return
        self._playback_finished = True
        await self._queue.put(None)

    @staticmethod
    def _parse_pcm_format(pcm_format: str, sampwidth: int) -> Tuple[bool, str]:
        fmt = (pcm_format or "").strip().lower()
        signed = True
        endian = "<"
        if fmt.startswith("u"):
            signed = False
        elif fmt.startswith("s"):
            signed = True
        else:
            if sampwidth == 1:
                signed = False
        if fmt.endswith("be"):
            endian = ">"
        elif fmt.endswith("le"):
            endian = "<"
        return signed, endian

    @staticmethod
    def _resolve_pcm_dtype(sampwidth: int, signed: bool, endian: str):
        if sampwidth == 1:
            return np.int8 if signed else np.uint8
        if sampwidth == 2:
            code = "i2" if signed else "u2"
            return np.dtype(f"{endian}{code}")
        if sampwidth == 4:
            code = "i4" if signed else "u4"
            return np.dtype(f"{endian}{code}")
        return None

    @staticmethod
    def _pcm_scale_offset(sampwidth: int, signed: bool) -> Tuple[float, float]:
        if signed:
            scale = float(2 ** (8 * sampwidth - 1) - 1)
            return scale, 0.0
        offset = float(2 ** (8 * sampwidth - 1))
        return float(offset), offset
