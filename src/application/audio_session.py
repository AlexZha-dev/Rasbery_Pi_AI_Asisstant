from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import numpy as np

from application.playback_pipeline import PlaybackFormatSupport, PlaybackPipeline
from domain.session_contracts import (
    SERVER_DRIVEN_EVENT_TYPES,
    SessionBinding,
    SessionPhase,
)
from dto.audio_message import MAX_AUDIO_ARRAY_BYTES, base64_to_np
from exceptions.audio_exceptions import AudioError
from infrastructure.websocket_client import AudioWebSocketClient


class AudioSession:
    MAX_JSON_AUDIO_BYTES = MAX_AUDIO_ARRAY_BYTES
    MAX_PLAYBACK_FILE_BYTES = 16 * 1024 * 1024
    MAX_CHANNELS = PlaybackFormatSupport.MAX_CHANNELS
    MAX_SAMPLE_RATE = PlaybackFormatSupport.MAX_SAMPLE_RATE

    def __init__(self, ws: AudioWebSocketClient, mic_adapter, spk_adapter):
        self.ws = ws
        self.mic = mic_adapter
        self.spk = spk_adapter

        self._session_id = str(uuid.uuid4())
        self._binding = SessionBinding(client_session_id=self._session_id)
        self._phase = SessionPhase.IDLE

        self._queue: "asyncio.Queue[Optional[np.ndarray]]" = asyncio.Queue()
        self._playback_finished = False
        self._pending_playback_files = 0
        self._playback_session_active = False
        self._final_event_received = False
        self._last_activity = time.monotonic()
        self._playback_complete_event: Optional[asyncio.Event] = None
        self._target_playback_sample_rate = 16000
        self._playback_pipeline = PlaybackPipeline(
            target_sample_rate=self._target_playback_sample_rate,
            max_file_bytes=self.MAX_PLAYBACK_FILE_BYTES,
        )

        ws._on_receive = self._on_receive
        ws._on_receive_binary = self._on_binary

    @property
    def session_id(self) -> str:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        normalized = str(value or "").strip() or str(uuid.uuid4())
        self._session_id = normalized
        self._binding.client_session_id = normalized

    @property
    def active_session_id(self) -> str:
        return self._binding.active_session_id

    @property
    def server_session_id(self) -> Optional[str]:
        return self._binding.server_session_id

    @property
    def phase(self) -> SessionPhase:
        return self._phase

    def _resample(
        self, arr: np.ndarray, src_rate: float, dst_rate: float
    ) -> np.ndarray:
        return PlaybackFormatSupport.resample(arr, src_rate, dst_rate)

    async def _on_receive(self, msg):
        self._last_activity = time.monotonic()
        if not self._accepts_message(msg):
            return

        if msg.type == "audio_chunk":
            try:
                data = base64_to_np(
                    msg.data_b64,
                    msg.dtype,
                    msg.shape,
                    max_decoded_bytes=self.MAX_JSON_AUDIO_BYTES,
                )
            except Exception:
                return
            self._phase = SessionPhase.PLAYING
            await self._queue_audio(np.asarray(data, dtype=np.float32))
        elif msg.type == "response.start":
            self._phase = SessionPhase.WAITING_RESPONSE
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
            "playback_stopped",
            "stop_playback",
        }:
            self._final_event_received = True
            await self._maybe_complete_playback()
        elif msg.type == "playback.start":
            self._playback_session_active = True
            self._phase = SessionPhase.PLAYING
        elif msg.type == "playback.end":
            self._playback_session_active = False
            self._final_event_received = True
            await self._maybe_complete_playback()

    async def run_once(
        self,
        timeout: Optional[float] = None,
        playback_timeout: Optional[float] = None,
    ):
        print(f"[Session {self.active_session_id}] Started")
        self._phase = SessionPhase.CONNECTING

        sample_rate = int(getattr(self.mic.mic, "samplerate", 16000) or 16000)
        chunk_frames = int(getattr(self.mic.mic, "blocksize", 1024) or 1024)
        channels = int(getattr(self.mic.mic, "channels", 1) or 1)
        self._target_playback_sample_rate = int(
            getattr(getattr(self.spk, "spk", None), "samplerate", sample_rate)
            or sample_rate
        )
        self._playback_pipeline = PlaybackPipeline(
            target_sample_rate=self._target_playback_sample_rate,
            max_file_bytes=self.MAX_PLAYBACK_FILE_BYTES,
        )
        self.ws.configure_stream(
            sample_rate=sample_rate,
            chunk_frames=chunk_frames,
            channels=channels,
            sampwidth=2,
        )
        await self.ws.connect()
        self._playback_finished = False
        self._pending_playback_files = 0
        self._playback_session_active = False
        self._final_event_received = False
        self._playback_complete_event = asyncio.Event()
        self._phase = SessionPhase.RECORDING
        try:
            await self.ws.prepare_stream(
                self.session_id,
                sample_rate=sample_rate,
                chunk_frames=chunk_frames,
                channels=channels,
                sampwidth=2,
            )
        except AttributeError:
            pass
        reset_speaker = getattr(self.spk, "reset", None)
        if callable(reset_speaker):
            try:
                reset_speaker()
            except Exception:
                pass
        stop_output = getattr(self.spk.spk, "stop_output", None)
        if callable(stop_output):
            try:
                stop_output()
            except Exception:
                pass
        self.spk.spk.start_output()
        try:
            self.mic.mic.start_recording()
        except Exception:
            self.spk.spk.stop_output()
            self._phase = SessionPhase.ERROR
            raise

        sender = asyncio.create_task(self._send_loop())
        run_error: Optional[Exception] = None
        try:
            try:
                if timeout is not None:
                    await asyncio.wait_for(self._recording_done(), timeout)
                else:
                    await self._recording_done()
            except asyncio.TimeoutError:
                pass
        except Exception as exc:  # pragma: no cover - unexpected flow
            run_error = exc
        finally:
            self.mic.mic.stop_recording()

        try:
            self._phase = SessionPhase.SENDING
            if playback_timeout is not None:
                await asyncio.wait_for(sender, timeout=playback_timeout)
            else:
                await sender
        except asyncio.CancelledError:
            self._phase = SessionPhase.ERROR
            raise
        except asyncio.TimeoutError as exc:
            sender.cancel()
            if run_error is None:
                run_error = AudioError("Sender task timed out")
                run_error.__cause__ = exc
        except Exception as exc:
            if run_error is None:
                run_error = exc

        try:
            self._phase = SessionPhase.WAITING_RESPONSE
            await self.ws.send_control(self.active_session_id, "end_session")
            await self.ws.send_playback(self.active_session_id, mode="background")
        except Exception as exc:
            if run_error is None:
                run_error = exc

        try:
            if run_error is None:
                poll = 1.0
                idle_limit = (
                    float(playback_timeout) if playback_timeout is not None else None
                )
                while True:
                    try:
                        data = await asyncio.wait_for(self._queue.get(), timeout=poll)
                    except asyncio.TimeoutError:
                        now = time.monotonic()
                        if self._playback_finished:
                            break
                        if (
                            idle_limit is not None
                            and (now - self._last_activity) > idle_limit
                        ):
                            print(
                                f"[Session {self.active_session_id}] playback idle > {idle_limit}s; stopping"
                            )
                            self._final_event_received = True
                            await self._maybe_complete_playback()
                            break
                        continue
                    if data is None:
                        break
                    self._phase = SessionPhase.PLAYING
                    await self.spk.play(data)
                print(f"[Session {self.active_session_id}] Completed")
        finally:
            flush_playback = getattr(self.spk, "flush", None)
            if callable(flush_playback):
                try:
                    maybe_result = flush_playback(timeout=1.5)
                    if asyncio.iscoroutine(maybe_result):
                        await maybe_result
                except TypeError:
                    try:
                        maybe_result = flush_playback()
                        if asyncio.iscoroutine(maybe_result):
                            await maybe_result
                    except Exception:
                        pass
                except Exception:
                    pass
            try:
                await self._await_speaker_drain(
                    timeout=self._speaker_drain_timeout(playback_timeout)
                )
            except Exception:
                pass
            self.spk.spk.stop_output()
            if (
                run_error is not None
                and self._playback_complete_event
                and not self._playback_complete_event.is_set()
            ):
                self._playback_complete_event.set()

        if run_error is not None:
            self._phase = SessionPhase.ERROR
            raise run_error
        self._phase = SessionPhase.COMPLETED

    async def _send_loop(self):
        while self.mic.mic.is_recording:
            samples = await self.mic.get_samples()
            if samples is not None:
                await self.ws.send_audio_chunk(self.active_session_id, samples)
        idle_after_stop = 0
        max_idle_iters = 100
        while idle_after_stop < max_idle_iters:
            samples = await self.mic.get_samples()
            if samples is None:
                idle_after_stop += 1
                continue
            idle_after_stop = 0
            await self.ws.send_audio_chunk(self.active_session_id, samples)
        print(f"[Session {self.active_session_id}] Sender finished")

    async def _recording_done(self):
        while self.mic.mic.is_recording:
            await asyncio.sleep(0.05)

    async def _handle_playback_start(self, msg):
        state = self._playback_pipeline.start_file(msg.extra or {})
        if state is None:
            return
        self._pending_playback_files += 1
        self._phase = SessionPhase.PLAYING

    async def _handle_playback_done(self):
        result = self._playback_pipeline.finish_file()
        if result.audio is not None:
            await self._queue_audio(result.audio)
        if self._pending_playback_files > 0:
            self._pending_playback_files -= 1
        await self._maybe_send_playback_ack(
            result.message_id,
            status="dropped" if result.dropped else "played",
        )
        await self._maybe_complete_playback()

    async def _on_binary(self, payload: bytes):
        self._last_activity = time.monotonic()
        result = self._playback_pipeline.feed_binary(payload)
        if result.audio is not None:
            self._phase = SessionPhase.PLAYING
            await self._queue_audio(result.audio)

    def _build_pcm_meta(
        self,
        *,
        channels: int,
        sampwidth: int,
        pcm_format: str,
        sample_rate: int,
    ) -> Optional[Dict[str, Any]]:
        return PlaybackFormatSupport.build_pcm_meta(
            channels=channels,
            sampwidth=sampwidth,
            pcm_format=pcm_format,
            sample_rate=sample_rate,
        )

    async def _flush_wav_buffer(self, buffer: bytearray) -> None:
        decoded = PlaybackFormatSupport.decode_wav_bytes(bytes(buffer))
        if decoded is None:
            return
        arr, wav_meta = decoded
        if int(wav_meta["sample_rate"]) != self._target_playback_sample_rate:
            arr = self._resample(
                arr,
                float(wav_meta["sample_rate"]),
                float(self._target_playback_sample_rate),
            )
        await self._queue_audio(arr)

    def _decode_pcm_bytes(
        self, payload: bytes, meta: Dict[str, Any]
    ) -> Optional[np.ndarray]:
        return PlaybackFormatSupport.decode_pcm_bytes(
            payload,
            dtype=meta.get("dtype"),
            channels=int(meta.get("channels", 1)),
            offset=float(meta.get("offset", 0.0)),
            scale=float(meta.get("scale") or 1.0),
            sample_kind=str(meta.get("sample_kind") or "int"),
        )

    async def _maybe_complete_playback(self) -> None:
        if self._playback_finished:
            return
        if self._pending_playback_files > 0:
            return
        if self._playback_pipeline.current_file is not None:
            return
        if not self._final_event_received:
            return
        self._playback_finished = True
        self._phase = SessionPhase.COMPLETED
        await self._queue.put(None)
        if self._playback_complete_event and not self._playback_complete_event.is_set():
            self._playback_complete_event.set()

    async def _maybe_send_playback_ack(
        self, message_id: Optional[str], status: str = "played"
    ) -> None:
        if not message_id:
            return
        try:
            await self._await_speaker_drain(timeout=self._speaker_drain_timeout(None))
        except Exception:
            pass
        try:
            await self.ws.send_playback_ack(
                self.active_session_id, message_id, status=status
            )
        except Exception:
            pass

    async def _queue_audio(self, audio: np.ndarray) -> None:
        arr = np.asarray(audio, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        await self._queue.put(arr)

    def _accepts_message(self, msg) -> bool:
        session_id = getattr(msg, "session_id", None)
        if session_id in {None, ""}:
            return True
        if self._binding.matches(session_id):
            return True
        if getattr(msg, "type", None) in SERVER_DRIVEN_EVENT_TYPES:
            self._binding.bind(session_id)
            return self._binding.matches(session_id)
        return False

    async def _await_speaker_drain(self, timeout: float = 3.0) -> None:
        get_pending_frames = getattr(self.spk.spk, "pending_frames", None)
        get_pending = getattr(self.spk.spk, "pending_blocks", None)
        if not callable(get_pending_frames) and not callable(get_pending):
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if callable(get_pending_frames):
                remaining = int(get_pending_frames())
            else:
                remaining = int(get_pending()) if callable(get_pending) else 0
            if remaining <= 0:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(self._speaker_tail_seconds())

    async def wait_for_playback_completion(
        self, timeout: Optional[float] = None
    ) -> bool:
        """Wait until the playback pipeline has fully completed.

        This is primarily used during shutdown/cleanup to ensure we have
        received and processed any remaining playback events before the
        websocket is closed.
        """
        event = self._playback_complete_event
        if event is None or event.is_set():
            return True
        awaitable = asyncio.shield(event.wait())
        if timeout is None:
            try:
                await awaitable
                return True
            except asyncio.CancelledError:
                raise
        else:
            try:
                await asyncio.wait_for(awaitable, timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False
        return True

    def _speaker_tail_seconds(self) -> float:
        speaker = getattr(self.spk, "spk", None)
        if speaker is None:
            return 0.05
        try:
            blocksize = int(getattr(speaker, "blocksize", 1024))
            samplerate = float(getattr(speaker, "samplerate", 16000))
        except (TypeError, ValueError):
            return 0.05
        if blocksize <= 0 or samplerate <= 0:
            return 0.05
        return min(0.25, max(0.05, blocksize / samplerate))

    def _speaker_drain_timeout(self, playback_timeout: Optional[float]) -> float:
        base_timeout = 10.0
        if playback_timeout is not None:
            base_timeout = max(base_timeout, float(playback_timeout))
        speaker = getattr(self.spk, "spk", None)
        if speaker is None:
            return base_timeout
        get_pending_frames = getattr(speaker, "pending_frames", None)
        if not callable(get_pending_frames):
            return base_timeout
        try:
            pending_frames = int(get_pending_frames())
            samplerate = float(getattr(speaker, "samplerate", 16000))
        except (TypeError, ValueError):
            return base_timeout
        if pending_frames <= 0 or samplerate <= 0:
            return base_timeout
        estimated = (pending_frames / samplerate) + self._speaker_tail_seconds() + 0.5
        return max(base_timeout, estimated)

    @staticmethod
    def _normalized_format_token(value: Optional[str]) -> str:
        return PlaybackFormatSupport.normalized_format_token(value)

    @classmethod
    def _is_wav_format(cls, value: Optional[str]) -> bool:
        return PlaybackFormatSupport.is_wav_format(value)

    @classmethod
    def _looks_like_pcm_descriptor(cls, value: Optional[str]) -> bool:
        return PlaybackFormatSupport.looks_like_pcm_descriptor(value)

    @classmethod
    def _resolve_pcm_format(
        cls,
        *,
        raw_format: Optional[str],
        explicit_pcm_format: Optional[str],
        sample_format: Optional[str],
        encoding: Optional[str],
        codec: Optional[str],
    ) -> str:
        return PlaybackFormatSupport.resolve_pcm_format(
            raw_format=raw_format,
            explicit_pcm_format=explicit_pcm_format,
            sample_format=sample_format,
            encoding=encoding,
            codec=codec,
        )

    @classmethod
    def _resolve_playback_sampwidth(
        cls,
        *,
        explicit_sampwidth,
        pcm_format: str,
        channels: int,
        frame_size,
    ) -> int:
        return PlaybackFormatSupport.resolve_playback_sampwidth(
            explicit_sampwidth=explicit_sampwidth,
            pcm_format=pcm_format,
            channels=channels,
            frame_size=frame_size,
        )

    @staticmethod
    def _infer_sampwidth_from_frame_size(frame_size, channels: int) -> Optional[int]:
        return PlaybackFormatSupport.infer_sampwidth_from_frame_size(
            frame_size, channels
        )

    @classmethod
    def _infer_sampwidth_from_pcm_format(cls, pcm_format: str) -> Optional[int]:
        return PlaybackFormatSupport.infer_sampwidth_from_pcm_format(pcm_format)

    @classmethod
    def _parse_pcm_format(cls, pcm_format: str, sampwidth: int) -> Tuple[str, str]:
        return PlaybackFormatSupport.parse_pcm_format(pcm_format, sampwidth)

    @staticmethod
    def _resolve_pcm_dtype(sampwidth: int, sample_kind: str, endian: str):
        return PlaybackFormatSupport.resolve_pcm_dtype(sampwidth, sample_kind, endian)

    @staticmethod
    def _pcm_scale_offset(sampwidth: int, sample_kind: str) -> Tuple[float, float]:
        return PlaybackFormatSupport.pcm_scale_offset(sampwidth, sample_kind)
