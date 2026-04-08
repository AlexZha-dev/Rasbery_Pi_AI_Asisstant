import asyncio
import json
import logging
import random
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Callable, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np
import websockets
from websockets import WebSocketClientProtocol
from websockets.exceptions import InvalidStatus

from config.audio_config import AUDIO_WS_URL
from dto.audio_message import AudioMessage, np_to_base64

logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

_SENSITIVE_HEADER_MARKERS = (
    "authorization",
    "cookie",
    "set-cookie",
    "token",
    "secret",
    "api-key",
    "x-api-key",
    "proxy-authorization",
)

_BINARY_AUDIO_PATH = "/ws/audio"
_DEFAULT_STREAM_SAMPLE_RATE = 16000
_DEFAULT_STREAM_CHUNK_FRAMES = 1024
_DEFAULT_STREAM_CHANNELS = 1
_DEFAULT_STREAM_SAMPWIDTH = 2


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _truncate_text(text: str, limit: int = 160) -> str:
    if text is None:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _headers_to_dict(headers) -> Dict[str, str]:
    if headers is None:
        return {}
    try:
        items = headers.raw_items()
    except AttributeError:
        try:
            items = headers.items()
        except AttributeError:
            return {}
    result: Dict[str, str] = {}
    for key, value in items:
        result[str(key)] = str(value)
    return result


def _is_sensitive_header(header_name: str) -> bool:
    lowered = header_name.strip().lower()
    return any(marker in lowered for marker in _SENSITIVE_HEADER_MARKERS)


def _mask_secret(value: str) -> str:
    if value is None:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}***{value[-2:]}"


def _sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    masked: Dict[str, str] = {}
    for key, value in headers.items():
        if _is_sensitive_header(key):
            masked[key] = _mask_secret(value)
        else:
            masked[key] = value
    return masked


def _safe_url_for_logs(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except Exception:
        return "-"
    host = parsed.hostname or "-"
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    return f"{parsed.scheme}://{host}{port}{path}"


class AudioWebSocketClient:
    _SESSION_INDEX = 0
    _ACTIVE_SESSIONS = 0

    def __init__(
        self,
        url: Optional[str] = None,
        on_receive: Optional[Callable[[AudioMessage], None]] = None,
        mode: Optional[str] = None,  # 'json' or 'binary' (auto if None)
        max_frame_bytes: int = 4 * 1024 * 1024,
        connect_timeout: float = 10.0,
        max_retries: Optional[int] = 5,
        retry_backoff_base: float = 0.5,
        retry_backoff_max: float = 8.0,
        ready_timeout: float = 5.0,
        log_payload_snippets: bool = False,
    ):
        self.url = url or AUDIO_WS_URL
        self._ws: Optional[WebSocketClientProtocol] = None
        self._on_receive = on_receive
        self._on_receive_binary: Optional[Callable[[bytes], None]] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._closing = False
        self._explicit_mode = mode is not None
        self._mode = mode or self._infer_mode_from_url(self.url)
        self._max_frame_bytes = max(1024, int(max_frame_bytes))
        self._connect_timeout = max(0.1, float(connect_timeout))
        if max_retries is None:
            self._max_retries = None
        else:
            self._max_retries = max(1, int(max_retries))
        self._retry_backoff_base = max(0.1, float(retry_backoff_base))
        self._retry_backoff_max = max(self._retry_backoff_base, float(retry_backoff_max))
        self._ready_timeout = max(0.1, float(ready_timeout))
        self._log_payload_snippets = bool(log_payload_snippets)
        self._stream_sample_rate = _DEFAULT_STREAM_SAMPLE_RATE
        self._stream_chunk_frames = _DEFAULT_STREAM_CHUNK_FRAMES
        self._stream_channels = _DEFAULT_STREAM_CHANNELS
        self._stream_sampwidth = _DEFAULT_STREAM_SAMPWIDTH
        # Binary protocol state
        self._binary_started: bool = False
        self._binary_chunks_sent: int = 0
        self._binary_expected_sampwidth: int = 2  # bytes per sample (default 16-bit)
        self._binary_channels: int = 1
        self._binary_chunk_bytes: int = 0
        self._closed_by_server = False
        self._close_code: Optional[int] = None
        self._close_reason: Optional[str] = None
        self._server_close_rcvd: bool = False
        self._close_cause: Optional[str] = None
        self._close_trigger_source: Optional[str] = None
        self._close_trigger_detail: Optional[str] = None
        self._closed_at: Optional[float] = None

        # Diagnostics
        self._session_name: Optional[str] = None
        self._connected_at: Optional[float] = None
        self._handshake_request_headers: Dict[str, str] = {}
        self._handshake_response_headers: Dict[str, str] = {}
        self._handshake_subprotocol: Optional[str] = None
        self._reported_session_id: Optional[str] = None
        self._sent_frames: int = 0
        self._sent_bytes: int = 0
        self._recv_frames: int = 0
        self._recv_bytes: int = 0
        self._last_event_type: Optional[str] = None
        self._close_logged: bool = False
        self._initiator: Optional[str] = None

    # --------------------------------------------------------------------- utils
    def _current_session_label(self) -> str:
        return self._reported_session_id or self._session_name or "unbound"

    def _log(
        self, tag: str, message: str, level: int = logging.INFO, *, exc_info=False
    ):
        logger.log(
            level,
            f"[{tag}] ts={_utc_timestamp()} session={self._current_session_label()} {message}",
            exc_info=exc_info,
        )

    def note_close_trigger(self, source: str, detail: Optional[str] = None) -> None:
        parts = [f"source={source}"]
        if detail:
            parts.append(f"detail={detail}")
        if self._close_trigger_source is None:
            self._close_trigger_source = source
            self._close_trigger_detail = detail
        else:
            parts.append("ignored=True")
        self._log("WS_CLOSE_TRIGGER", " ".join(parts))

    def _log_frame(
        self,
        direction: str,
        frame_type: str,
        payload,
        *,
        session_id: Optional[str] = None,
        message_type: Optional[str] = None,
        extra: Optional[str] = None,
        level: int = logging.INFO,
    ) -> None:
        if isinstance(payload, (bytes, bytearray)):
            size = len(payload)
            snippet = None
        else:
            text = str(payload)
            size = len(text.encode("utf-8", "ignore"))
            snippet = _truncate_text(text) if self._log_payload_snippets else None
        parts = [direction, frame_type, f"len={size}"]
        if message_type:
            parts.append(f"type={message_type}")
        if session_id:
            parts.append(f"session_id={session_id}")
        if snippet:
            parts.append(f"snippet={snippet}")
        if extra:
            parts.append(extra)
        self._log("WS_MESSAGE", " ".join(parts), level=level)

    def _record_session_id(self, session_id: Optional[str]) -> None:
        if session_id:
            self._reported_session_id = session_id

    def _emit_close_log(
        self,
        *,
        initiator: str,
        code: Optional[int],
        reason: Optional[str],
        server_close: bool,
    ) -> None:
        if self._close_logged:
            return
        duration = None
        if self._connected_at is not None:
            duration = max(0.0, time.monotonic() - self._connected_at)
        if self._closed_at is None:
            self._closed_at = time.monotonic()
        detail_parts = [
            f"initiator={initiator}",
            f"code={code if code is not None else 'n/a'}",
            f"reason={reason or '-'}",
            f"cause={self._close_cause or '-'}",
            f"trigger={self._close_trigger_source or '-'}",
            f"server_close={server_close}",
        ]
        if self._close_trigger_detail:
            detail_parts.append(f"trigger_detail={self._close_trigger_detail}")
        if duration is not None:
            detail_parts.append(f"duration_s={duration:.3f}")
            detail_parts.append(f"duration_ms={duration * 1000:.1f}")
        if self._last_event_type:
            detail_parts.append(f"last_event={self._last_event_type}")
        self._log("WS_CLOSE", " ".join(detail_parts))

        if AudioWebSocketClient._ACTIVE_SESSIONS > 0:
            AudioWebSocketClient._ACTIVE_SESSIONS -= 1
        summary_session = self._current_session_label()
        summary_parts = [
            f"session={summary_session}",
            f"sent={self._sent_frames}",
            f"recv={self._recv_frames}",
            f"sent_bytes={self._sent_bytes}",
            f"recv_bytes={self._recv_bytes}",
            f"active={AudioWebSocketClient._ACTIVE_SESSIONS}",
        ]
        if duration is not None:
            summary_parts.append(f"duration_s={duration:.3f}")
        summary_parts.append(f"last_event={self._last_event_type or '-'}")
        if self._close_trigger_source:
            summary_parts.append(f"trigger={self._close_trigger_source}")
        if self._close_cause:
            summary_parts.append(f"cause={self._close_cause}")
        self._log("WS_SUMMARY", " ".join(summary_parts))
        self._close_logged = True

    # --------------------------------------------------------------------- core
    async def _send_text(
        self,
        payload: str,
        *,
        session_id: Optional[str] = None,
        message_type: Optional[str] = None,
        extra: Optional[str] = None,
    ) -> None:
        await self._ws.send(payload)
        self._sent_frames += 1
        self._sent_bytes += len(payload.encode("utf-8", "ignore"))
        self._log_frame(
            ">",
            "text",
            payload,
            session_id=session_id,
            message_type=message_type,
            extra=extra,
        )

    async def _send_binary(
        self,
        payload: bytes,
        *,
        session_id: Optional[str] = None,
        message_type: Optional[str] = None,
        extra: Optional[str] = None,
    ) -> None:
        await self._ws.send(payload)
        self._sent_frames += 1
        self._sent_bytes += len(payload)
        self._log_frame(
            ">",
            "binary",
            payload,
            session_id=session_id,
            message_type=message_type,
            extra=extra,
        )

    @staticmethod
    def _infer_mode_from_url(url: str) -> str:
        if AudioWebSocketClient._is_binary_endpoint_url(url):
            return "binary"
        return "json"

    @staticmethod
    def _normalized_path(path: str) -> str:
        normalized = (path or "").strip()
        if not normalized or normalized == "/":
            return "/"
        return normalized.rstrip("/") or "/"

    @classmethod
    def _is_binary_endpoint_url(cls, url: str) -> bool:
        try:
            path = cls._normalized_path(urlsplit(url).path)
        except Exception:
            return False
        return path == _BINARY_AUDIO_PATH

    @classmethod
    def _is_root_endpoint_url(cls, url: str) -> bool:
        try:
            path = cls._normalized_path(urlsplit(url).path)
        except Exception:
            return False
        return path == "/"

    @staticmethod
    def _merge_query_params(url: str, params: Dict[str, int]) -> str:
        parsed = urlsplit(url)
        existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key, value in params.items():
            existing.setdefault(key, str(value))
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path or "/",
                urlencode(existing),
                parsed.fragment,
            )
        )

    @staticmethod
    def _replace_path(url: str, path: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                path,
                parsed.query,
                parsed.fragment,
            )
        )

    @staticmethod
    def _status_code_from_exception(exc: Exception) -> Optional[int]:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code is None:
            return None
        try:
            return int(status_code)
        except (TypeError, ValueError):
            return None

    def configure_stream(
        self,
        *,
        sample_rate: int,
        chunk_frames: int,
        channels: int,
        sampwidth: int = 2,
    ) -> None:
        self._stream_sample_rate = max(1, int(sample_rate or _DEFAULT_STREAM_SAMPLE_RATE))
        self._stream_chunk_frames = max(
            1, int(chunk_frames or _DEFAULT_STREAM_CHUNK_FRAMES)
        )
        self._stream_channels = max(1, int(channels or _DEFAULT_STREAM_CHANNELS))
        self._stream_sampwidth = max(1, int(sampwidth or _DEFAULT_STREAM_SAMPWIDTH))

    def _binary_handshake_query_params(self) -> Dict[str, int]:
        return {
            "sample_rate": int(self._stream_sample_rate),
            "chunk_size": int(self._stream_chunk_frames),
            "channels": int(self._stream_channels),
            "bit_depth_bytes": int(self._stream_sampwidth),
        }

    def _build_connect_url(self, url: str, mode: str) -> str:
        connect_url = url
        if mode == "binary":
            if self._is_root_endpoint_url(connect_url):
                connect_url = self._replace_path(connect_url, _BINARY_AUDIO_PATH)
            connect_url = self._merge_query_params(
                connect_url, self._binary_handshake_query_params()
            )
        return connect_url

    def _build_connect_candidates(self):
        primary_url = self._build_connect_url(self.url, self._mode)
        candidates = [
            {
                "url": primary_url,
                "mode": self._mode,
                "is_fallback": False,
            }
        ]
        if (
            not self._explicit_mode
            and self._mode != "binary"
            and self._is_root_endpoint_url(self.url)
        ):
            fallback_url = self._build_connect_url(self.url, "binary")
            if fallback_url != primary_url:
                candidates.append(
                    {
                        "url": fallback_url,
                        "mode": "binary",
                        "is_fallback": True,
                    }
                )
        return candidates

    def _format_connect_error(self, url: str, exc: Exception) -> str:
        message = str(exc)
        status_code = self._status_code_from_exception(exc)
        if status_code == 403 and self._is_root_endpoint_url(url):
            expected = _safe_url_for_logs(self._build_connect_url(url, "binary"))
            return f"{message}; server likely expects {expected}"
        return message

    async def connect(self):
        self._closing = False
        self._closed_by_server = False
        self._close_code = None
        self._close_reason = None
        self._server_close_rcvd = False
        self._close_logged = False
        self._reported_session_id = None
        self._sent_frames = 0
        self._sent_bytes = 0
        self._recv_frames = 0
        self._recv_bytes = 0
        self._last_event_type = None
        self._handshake_request_headers = {}
        self._handshake_response_headers = {}
        self._handshake_subprotocol = None
        self._session_name = None
        self._connected_at = None
        self._initiator = None
        self._close_cause = None
        self._close_trigger_source = None
        self._close_trigger_detail = None
        self._closed_at = None
        self._ready_event.clear()
        self._connected.clear()
        attempt = 0
        while True:
            attempt += 1
            candidates = self._build_connect_candidates()
            last_exc: Optional[Exception] = None
            for index, candidate in enumerate(candidates, start=1):
                connect_url = str(candidate["url"])
                candidate_mode = str(candidate["mode"])
                safe_url = _safe_url_for_logs(connect_url)
                try:
                    self._ws = await websockets.connect(
                        connect_url,
                        max_size=self._max_frame_bytes,
                        open_timeout=self._connect_timeout,
                    )
                    self._mode = candidate_mode
                    AudioWebSocketClient._SESSION_INDEX += 1
                    self._session_name = f"ws-{AudioWebSocketClient._SESSION_INDEX}"
                    AudioWebSocketClient._ACTIVE_SESSIONS += 1
                    self._connected_at = time.monotonic()
                    self._handshake_request_headers = _sanitize_headers(
                        _headers_to_dict(getattr(self._ws, "request_headers", None))
                    )
                    self._handshake_response_headers = _sanitize_headers(
                        _headers_to_dict(getattr(self._ws, "response_headers", None))
                    )
                    self._handshake_subprotocol = getattr(self._ws, "subprotocol", None)
                    handshake_extra = {
                        "request_headers": self._handshake_request_headers,
                        "response_headers": self._handshake_response_headers,
                    }
                    if self._log_payload_snippets:
                        handshake_note = _truncate_text(json.dumps(handshake_extra))
                    else:
                        handshake_note = (
                            f"request_headers={len(self._handshake_request_headers)} "
                            f"response_headers={len(self._handshake_response_headers)}"
                        )
                    self._log(
                        "WS_OPEN",
                        " ".join(
                            [
                                f"url={safe_url}",
                                f"mode={self._mode}",
                                f"subprotocol={self._handshake_subprotocol or '-'}",
                                f"active={AudioWebSocketClient._ACTIVE_SESSIONS}",
                                f"max_frame_bytes={self._max_frame_bytes}",
                                f"handshake={handshake_note}",
                            ]
                        ),
                    )
                    self._connected.set()
                    self._recv_task = asyncio.create_task(self._receiver_loop())
                    return
                except Exception as exc:
                    last_exc = exc
                    should_try_fallback = (
                        index < len(candidates)
                        and not bool(candidate["is_fallback"])
                        and isinstance(exc, InvalidStatus)
                        and self._status_code_from_exception(exc) in {400, 401, 403, 404}
                    )
                    error_message = self._format_connect_error(connect_url, exc)
                    if should_try_fallback:
                        self._log(
                            "WS_ERROR",
                            (
                                f"connection_failed attempt={attempt} "
                                f"candidate={index}/{len(candidates)} "
                                f"url={safe_url} mode={candidate_mode} "
                                f"retrying_with_fallback=True error={error_message}"
                            ),
                            level=logging.WARNING,
                            exc_info=True,
                        )
                        continue
                    if self._max_retries is not None and attempt >= self._max_retries:
                        self._log(
                            "WS_ERROR",
                            (
                                f"connection_failed attempts={attempt} "
                                f"url={safe_url} mode={candidate_mode} "
                                f"giving_up=True error={error_message}"
                            ),
                            level=logging.ERROR,
                            exc_info=True,
                        )
                        raise ConnectionError(
                            f"Unable to connect to websocket after {attempt} attempts: "
                            f"{error_message}"
                        ) from exc
                    self._log(
                        "WS_ERROR",
                        (
                            f"connection_failed attempt={attempt} "
                            f"url={safe_url} mode={candidate_mode} error={error_message}"
                        ),
                        level=logging.WARNING,
                        exc_info=True,
                    )
                    break
            if last_exc is None:
                raise ConnectionError("Unable to connect to websocket")
            delay = min(
                self._retry_backoff_max,
                self._retry_backoff_base * (2 ** max(0, attempt - 1)),
            )
            jitter = random.uniform(0.0, min(0.2, delay * 0.2))
            await asyncio.sleep(delay + jitter)

    async def close(
        self, reason: str = "client_shutdown", trigger: Optional[str] = None
    ):
        if trigger and not self._close_trigger_source:
            self.note_close_trigger(trigger, detail=reason)
        elif not self._close_trigger_source:
            self.note_close_trigger("client.close", detail=reason)
        if not self._close_cause or self._close_cause in {
            "client_shutdown",
            "client_close",
        }:
            self._close_cause = reason or "client_shutdown"
        self._log("WS", f"close_requested cause={self._close_cause}")
        self._closing = True
        self._initiator = self._initiator or "client"
        if self._recv_task:
            self._recv_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._recv_task
            self._recv_task = None
        if self._ws is not None:
            close_coro = getattr(self._ws, "close", None)
            if callable(close_coro):
                try:
                    await close_coro()
                except Exception as exc:
                    self._log(
                        "WS_ERROR",
                        f"close_handshake_error error={exc}",
                        level=logging.ERROR,
                        exc_info=True,
                    )
            self._close_code = getattr(self._ws, "close_code", self._close_code)
            self._close_reason = getattr(self._ws, "close_reason", self._close_reason)
            server_close = self._server_close_rcvd or bool(
                getattr(self._ws, "close_rcvd", None)
            )
        else:
            server_close = self._server_close_rcvd
        self._emit_close_log(
            initiator=self._initiator or "client",
            code=self._close_code,
            reason=self._close_reason,
            server_close=server_close,
        )
        self._ws = None
        self._connected.clear()
        self._ready_event.clear()
        self._reset_binary_state()
        self._log(
            "WS_CLEANUP",
            " ".join(
                [
                    f"reason={reason}",
                    f"server_closed={self._closed_by_server}",
                    f"code={self._close_code}",
                    f"server_reason={self._close_reason or '-'}",
                ]
            ),
        )

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
            "chunk_size": int(chunk_frames),
            "chunk_size_bytes": int(chunk_bytes),
            "channels": channels,
            "sampwidth": sampwidth,
            "bit_depth_bytes": sampwidth,
        }
        payload = json.dumps(start_msg)
        self._record_session_id(session_id)
        await self._send_text(
            payload,
            session_id=session_id,
            message_type="start",
            extra=f"chunk_bytes={chunk_bytes}",
        )
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
            chunk_index = self._binary_chunks_sent + 1
            await self._send_binary(
                payload,
                session_id=session_id,
                message_type="audio_chunk",
                extra=f"index={chunk_index}",
            )
            if self._binary_chunks_sent == 0:
                self._log(
                    "WS",
                    f"> binary_first_chunk len={len(payload)} "
                    f"channels={self._binary_channels} sampwidth={self._binary_expected_sampwidth}",
                )
            self._binary_chunks_sent += 1
            if (self._binary_chunks_sent % 100) == 0:
                self._log(
                    "WS",
                    f"> binary_chunks_sent={self._binary_chunks_sent}",
                )
            return

        # JSON mode (legacy/test server)
        msg = AudioMessage(
            type="audio_chunk",
            session_id=session_id,
            data_b64=np_to_base64(frame),
            dtype=str(frame.dtype),
            shape=frame.shape,
        )
        payload = msg.to_json()
        self._record_session_id(session_id)
        await self._send_text(
            payload,
            session_id=session_id,
            message_type="audio_chunk",
            extra=f"dtype={msg.dtype} shape={msg.shape}",
        )

    async def send_control(self, session_id: str, control_type: str, **extra):
        await self._connected.wait()
        self._record_session_id(session_id)
        if self._mode == "binary" and control_type == "end_session":
            end_msg = {
                "type": "end_of_chunks",
                "session_id": session_id,
                "chunks_sent": int(self._binary_chunks_sent),
            }
            payload = json.dumps(end_msg)
            await self._send_text(
                payload,
                session_id=session_id,
                message_type="end_of_chunks",
                extra=f"chunks_sent={self._binary_chunks_sent}",
            )
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
        payload = msg.to_json()
        extra_str = (
            " ".join(f"{key}={value}" for key, value in sorted(extra.items()))
            if extra
            else None
        )
        await self._send_text(
            payload,
            session_id=session_id,
            message_type=target_type,
            extra=extra_str,
        )

    async def send_playback(self, session_id: str, mode: str = "background"):
        await self.send_control(session_id, "playback", mode=mode)
        self._log("WS", f"> playback_request mode={mode} session_id={session_id}")

    async def send_playback_stop(self, session_id: str):
        await self.send_control(session_id, "playback_stop")
        self._log("WS", f"> playback_stop session_id={session_id}")

    async def send_playback_ack(
        self, session_id: str, message_id: str, status: str = "played"
    ) -> None:
        await self._connected.wait()
        payload = {
            "type": "playback_ack",
            "message_id": message_id,
            "status": status,
        }
        self._record_session_id(session_id)
        await self._send_text(
            json.dumps(payload),
            session_id=session_id,
            message_type="playback_ack",
            extra=f"message_id={message_id} status={status}",
        )

    async def _receiver_loop(self):
        try:
            async for msg_raw in self._ws:
                if isinstance(msg_raw, (bytes, bytearray)):
                    self._recv_frames += 1
                    self._recv_bytes += len(msg_raw)
                    self._log_frame(
                        "<",
                        "binary",
                        msg_raw,
                        message_type="audio_binary",
                    )
                    self._last_event_type = "binary_frame"
                    if self._on_receive_binary:
                        result = self._on_receive_binary(bytes(msg_raw))
                        if asyncio.iscoroutine(result):
                            try:
                                await result
                            except Exception as exc:
                                self._log(
                                    "WS_ERROR",
                                    f"binary_handler_error error={exc}",
                                    level=logging.ERROR,
                                    exc_info=True,
                                )
                    continue

                if isinstance(msg_raw, str):
                    self._recv_frames += 1
                    self._recv_bytes += len(msg_raw.encode("utf-8", "ignore"))
                    stripped = (msg_raw or "").strip().lower()
                    if stripped == "ready":
                        self._ready_event.set()
                        self._last_event_type = "ready"
                        self._log_frame("<", "text", msg_raw, message_type="ready")
                        continue
                    if stripped.startswith("ack:"):
                        self._last_event_type = "ack_signal"
                        self._log_frame("<", "text", msg_raw, message_type="ack")
                        self._log_ack(stripped)
                        continue

                try:
                    msg = AudioMessage.from_json(msg_raw)
                except Exception:
                    if isinstance(msg_raw, str):
                        self._log_frame("<", "text", msg_raw)
                    continue

                session_id = msg.session_id
                self._record_session_id(session_id)
                message_type = msg.type or "unknown"
                extra = None
                if msg.extra:
                    try:
                        extra = f"extra={_truncate_text(json.dumps(msg.extra))}"
                    except (TypeError, ValueError):
                        extra = "extra=<unserializable>"

                if self._is_heartbeat_message(msg):
                    self._last_event_type = "heartbeat"
                    continue

                self._log_frame(
                    "<",
                    "text",
                    msg_raw,
                    session_id=session_id,
                    message_type=message_type,
                    extra=extra,
                )
                self._last_event_type = message_type

                if msg.type == "ready":
                    self._ready_event.set()
                    continue
                if msg.type and msg.type.startswith("ack"):
                    self._log_ack(msg.type)
                    continue
                if msg.type in {
                    "response.start",
                    "response.end",
                    "playback.start",
                    "playback.end",
                    "playback_file_start",
                    "playback_file_done",
                    "playback.queue_status",
                    "playback_stopped",
                }:
                    # Lightweight debug hook
                    self._log(
                        "WS",
                        f"< {msg.type} session_id={msg.session_id}",
                    )

                if self._on_receive:
                    try:
                        await self._on_receive(msg)
                    except Exception as exc:
                        self._log(
                            "WS_ERROR",
                            f"message_handler_error error={exc}",
                            level=logging.ERROR,
                            exc_info=True,
                        )
        except websockets.ConnectionClosed as exc:
            self._close_code = getattr(exc, "code", self._close_code)
            self._close_reason = getattr(exc, "reason", self._close_reason)
            self._server_close_rcvd = bool(getattr(exc, "rcvd", None))
            initiator = "client" if self._closing else "server"
            if not self._closing:
                self._closed_by_server = True
                self._close_cause = self._close_cause or "server_close"
                if not self._close_trigger_source:
                    detail = None
                    if self._close_reason:
                        detail = self._close_reason
                    elif self._close_code is not None:
                        detail = f"code={self._close_code}"
                    self.note_close_trigger("server.close_frame", detail=detail)
            else:
                self._close_cause = self._close_cause or "client_close"
            self._initiator = self._initiator or initiator
            self._emit_close_log(
                initiator=initiator,
                code=self._close_code,
                reason=self._close_reason,
                server_close=self._server_close_rcvd,
            )
        except Exception as exc:
            if not self._closing:
                self._close_cause = self._close_cause or "error"
                if not self._close_trigger_source:
                    self.note_close_trigger("receiver_loop", detail=str(exc))
                self._log(
                    "WS_ERROR",
                    f"receiver_loop_error error={exc}",
                    level=logging.ERROR,
                    exc_info=True,
                )
        finally:
            self._connected.clear()
            self._ready_event.clear()
            self._reset_binary_state()

    def _reset_binary_state(self) -> None:
        self._binary_started = False
        self._binary_chunks_sent = 0
        self._binary_chunk_bytes = 0
        self._binary_expected_sampwidth = 2
        self._binary_channels = 1

    async def _wait_for_ready(self, timeout: Optional[float] = None) -> None:
        if self._ready_event.is_set():
            return
        wait_timeout = self._ready_timeout if timeout is None else float(timeout)
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError as exc:
            self._log(
                "WS_ERROR",
                f"ready_wait_timeout timeout={wait_timeout}",
                level=logging.ERROR,
            )
            raise TimeoutError(
                f"Server did not send ready signal within {wait_timeout:.2f}s"
            ) from exc

    def _log_ack(self, payload: str) -> None:
        try:
            count = int(payload.split(":", 1)[1])
        except (IndexError, ValueError):
            return
        self._last_event_type = "ack"
        self._log("WS_MESSAGE", f"< ack count={count}")

    @staticmethod
    def _is_heartbeat_message(msg: AudioMessage) -> bool:
        if (msg.type or "").strip().lower() == "heartbeat":
            return True
        extra = msg.extra or {}
        event = str(extra.get("event") or "").strip().lower()
        return event == "heartbeat"

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
            self._log(
                "WS_ERROR",
                f"unsupported_sampwidth={sampwidth}",
                level=logging.WARNING,
            )
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
