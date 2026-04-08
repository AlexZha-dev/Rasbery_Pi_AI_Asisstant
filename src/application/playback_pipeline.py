from __future__ import annotations

import io
import re
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import numpy as np


@dataclass
class PlaybackChunkResult:
    audio: Optional[np.ndarray] = None
    message_id: Optional[str] = None
    dropped: bool = False


@dataclass
class PlaybackFileState:
    format: str
    message_id: str
    channels: int = 1
    sample_rate: int = 16000
    sampwidth: int = 2
    pcm_format: str = "s16le"
    dtype: Optional[np.dtype] = None
    scale: float = 1.0
    offset: float = 0.0
    sample_kind: str = "int"
    max_bytes: int = 16 * 1024 * 1024
    bytes_received: int = 0
    dropped: bool = False
    drop_reason: str = ""
    buffer: bytearray = field(default_factory=bytearray)


class PlaybackFormatSupport:
    MAX_CHANNELS = 8
    MAX_SAMPLE_RATE = 192000

    @staticmethod
    def resample(
        arr: np.ndarray, src_rate: float, dst_rate: float, dtype: str = "float32"
    ) -> np.ndarray:
        if not isinstance(arr, np.ndarray) or arr.size == 0:
            return np.asarray(arr, dtype=dtype)
        if src_rate == dst_rate:
            return arr.astype(dtype, copy=False)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        ratio = float(dst_rate) / float(src_rate)
        dst_len = max(1, int(round(arr.shape[0] * ratio)))
        orig_positions = np.linspace(0.0, 1.0, arr.shape[0], endpoint=False)
        new_positions = np.linspace(0.0, 1.0, dst_len, endpoint=False)
        resampled = np.empty((dst_len, arr.shape[1]), dtype=np.float32)
        for idx in range(arr.shape[1]):
            resampled[:, idx] = np.interp(new_positions, orig_positions, arr[:, idx])
        return resampled.astype(dtype)

    @staticmethod
    def normalized_format_token(value: Optional[str]) -> str:
        return str(value or "").strip().lower().replace("_", "").replace("-", "")

    @classmethod
    def is_wav_format(cls, value: Optional[str]) -> bool:
        token = cls.normalized_format_token(value)
        return token in {"wav", "wave", "audiowav", "audioxwav"}

    @classmethod
    def looks_like_pcm_descriptor(cls, value: Optional[str]) -> bool:
        token = cls.normalized_format_token(value)
        if not token:
            return False
        if token in {"pcm", "raw", "lpcm"}:
            return True
        if "float" in token and any(ch.isdigit() for ch in token):
            return True
        if token.startswith(("s", "u", "f")) and any(ch.isdigit() for ch in token):
            return True
        if token.startswith("pcm") and any(ch.isdigit() for ch in token):
            return True
        return False

    @classmethod
    def resolve_pcm_format(
        cls,
        *,
        raw_format: Optional[str],
        explicit_pcm_format: Optional[str],
        sample_format: Optional[str],
        encoding: Optional[str],
        codec: Optional[str],
    ) -> str:
        for candidate in (
            explicit_pcm_format,
            sample_format,
            encoding,
            codec,
        ):
            if candidate:
                return str(candidate).strip().lower()
        if cls.looks_like_pcm_descriptor(raw_format):
            return str(raw_format).strip().lower()
        return "s16le"

    @staticmethod
    def infer_sampwidth_from_frame_size(
        frame_size: Optional[Any], channels: int
    ) -> Optional[int]:
        try:
            frame_size_int = int(frame_size)
            channels_int = max(1, int(channels))
        except (TypeError, ValueError):
            return None
        if frame_size_int <= 0 or channels_int <= 0:
            return None
        if (frame_size_int % channels_int) != 0:
            return None
        return frame_size_int // channels_int

    @classmethod
    def infer_sampwidth_from_pcm_format(cls, pcm_format: str) -> Optional[int]:
        token = cls.normalized_format_token(pcm_format)
        match = re.search(r"(8|16|24|32|64)", token)
        if match:
            bits = int(match.group(1))
            if bits % 8 == 0:
                return bits // 8
        if token in {"u8", "s8"}:
            return 1
        return None

    @classmethod
    def resolve_playback_sampwidth(
        cls,
        *,
        explicit_sampwidth: Optional[Any],
        pcm_format: str,
        channels: int,
        frame_size: Optional[Any],
    ) -> int:
        if explicit_sampwidth is not None:
            return int(explicit_sampwidth)
        inferred = cls.infer_sampwidth_from_frame_size(frame_size, channels)
        if inferred is not None:
            return inferred
        inferred = cls.infer_sampwidth_from_pcm_format(pcm_format)
        if inferred is not None:
            return inferred
        return 2

    @classmethod
    def parse_pcm_format(cls, pcm_format: str, sampwidth: int) -> Tuple[str, str]:
        fmt = cls.normalized_format_token(pcm_format)
        sample_kind = "int"
        endian = "<"
        if "float" in fmt or fmt.startswith("f") or fmt.startswith("pcmf"):
            sample_kind = "float"
        elif fmt.startswith("u") or fmt.startswith("pcmu"):
            sample_kind = "uint"
        elif fmt.startswith("s") or fmt.startswith("pcms"):
            sample_kind = "int"
        elif sampwidth == 1:
            sample_kind = "uint"
        if fmt.endswith("be"):
            endian = ">"
        elif fmt.endswith("le"):
            endian = "<"
        return sample_kind, endian

    @staticmethod
    def resolve_pcm_dtype(sampwidth: int, sample_kind: str, endian: str):
        if sample_kind == "float":
            if sampwidth in {2, 4, 8}:
                return np.dtype(f"{endian}f{sampwidth}")
            return None
        if sampwidth == 1:
            return np.int8 if sample_kind == "int" else np.uint8
        if sampwidth == 2:
            code = "i2" if sample_kind == "int" else "u2"
            return np.dtype(f"{endian}{code}")
        if sampwidth == 4:
            code = "i4" if sample_kind == "int" else "u4"
            return np.dtype(f"{endian}{code}")
        return None

    @staticmethod
    def pcm_scale_offset(sampwidth: int, sample_kind: str) -> Tuple[float, float]:
        if sample_kind == "float":
            return 1.0, 0.0
        if sample_kind == "int":
            scale = float(2 ** (8 * sampwidth - 1) - 1)
            return scale, 0.0
        offset = float(2 ** (8 * sampwidth - 1))
        return float(offset), offset

    @classmethod
    def build_pcm_meta(
        cls,
        *,
        channels: int,
        sampwidth: int,
        pcm_format: str,
        sample_rate: int,
    ) -> Optional[Dict[str, Any]]:
        channels = int(channels)
        sample_rate = int(sample_rate)
        sampwidth = int(sampwidth)
        if channels < 1 or channels > cls.MAX_CHANNELS:
            return None
        if sample_rate < 1000 or sample_rate > cls.MAX_SAMPLE_RATE:
            return None
        sample_kind, endian = cls.parse_pcm_format(pcm_format, sampwidth)
        if sample_kind == "float":
            if sampwidth not in {2, 4, 8}:
                return None
        elif sampwidth not in {1, 2, 4}:
            return None
        dtype = cls.resolve_pcm_dtype(sampwidth, sample_kind, endian)
        if dtype is None:
            return None
        scale, offset = cls.pcm_scale_offset(sampwidth, sample_kind)
        return {
            "dtype": dtype,
            "scale": scale,
            "offset": offset,
            "channels": channels,
            "sample_rate": sample_rate,
            "sampwidth": sampwidth,
            "sample_kind": sample_kind,
            "pcm_format": pcm_format,
        }

    @staticmethod
    def decode_pcm_bytes(
        payload: bytes,
        *,
        dtype,
        channels: int,
        offset: float,
        scale: float,
        sample_kind: str,
    ) -> Optional[np.ndarray]:
        if not payload or dtype is None:
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
        if offset:
            arr = arr - offset
        if scale:
            arr = arr / scale
        if sample_kind == "float":
            arr = np.clip(arr, -1.0, 1.0)
        return arr

    @classmethod
    def decode_wav_bytes(cls, payload: bytes) -> Optional[Tuple[np.ndarray, Dict[str, int]]]:
        if not payload:
            return None
        try:
            with wave.open(io.BytesIO(payload), "rb") as wav_reader:
                channels = wav_reader.getnchannels()
                sampwidth = wav_reader.getsampwidth()
                sample_rate = wav_reader.getframerate()
                frame_count = wav_reader.getnframes()
                raw = wav_reader.readframes(frame_count)
        except wave.Error:
            return None
        pcm_format = "u8" if sampwidth == 1 else f"s{8 * sampwidth}le"
        meta = cls.build_pcm_meta(
            channels=channels,
            sampwidth=sampwidth,
            pcm_format=pcm_format,
            sample_rate=sample_rate,
        )
        if meta is None:
            return None
        arr = cls.decode_pcm_bytes(
            raw,
            dtype=meta["dtype"],
            channels=int(meta["channels"]),
            offset=float(meta["offset"]),
            scale=float(meta["scale"]),
            sample_kind=str(meta["sample_kind"]),
        )
        if arr is None:
            return None
        return arr, {
            "channels": channels,
            "sampwidth": sampwidth,
            "sample_rate": sample_rate,
        }


class PlaybackPipeline:
    def __init__(
        self,
        *,
        target_sample_rate: int = 16000,
        max_file_bytes: int = 16 * 1024 * 1024,
    ):
        self._target_sample_rate = max(1000, int(target_sample_rate))
        self._max_file_bytes = max(1024, int(max_file_bytes))
        self._current_file: Optional[PlaybackFileState] = None

    @property
    def current_file(self) -> Optional[PlaybackFileState]:
        return self._current_file

    def reset(self) -> None:
        self._current_file = None

    def start_file(self, metadata: Optional[Dict[str, Any]]) -> Optional[PlaybackFileState]:
        metadata = metadata or {}
        file_field = metadata.get("file")
        params = metadata.get("params") if isinstance(metadata.get("params"), dict) else {}
        meta_sources = []
        if isinstance(file_field, dict):
            meta_sources.append(file_field)
        if isinstance(metadata, dict):
            meta_sources.append(metadata)
        if isinstance(params, dict):
            meta_sources.append(params)

        def first(key: str, default=None):
            for source in meta_sources:
                value = source.get(key)
                if value is not None:
                    return value
            return default

        raw_format = str(first("format", "pcm")).strip().lower()
        message_id = self._extract_message_id(metadata, file_field)

        if PlaybackFormatSupport.is_wav_format(raw_format):
            self._current_file = PlaybackFileState(
                format="wav",
                message_id=message_id,
                max_bytes=self._max_file_bytes,
            )
            return self._current_file

        try:
            sample_rate = int(first("sample_rate", self._target_sample_rate))
            channels = int(first("channels", 1))
        except (TypeError, ValueError):
            self._current_file = None
            return None

        pcm_format = PlaybackFormatSupport.resolve_pcm_format(
            raw_format=raw_format,
            explicit_pcm_format=first("pcm_format"),
            sample_format=first("sample_format"),
            encoding=first("encoding"),
            codec=first("codec"),
        )
        try:
            sampwidth = PlaybackFormatSupport.resolve_playback_sampwidth(
                explicit_sampwidth=first("sampwidth", first("bit_depth_bytes")),
                pcm_format=pcm_format,
                channels=channels,
                frame_size=first("frame_size"),
            )
        except (TypeError, ValueError):
            self._current_file = None
            return None

        pcm_meta = PlaybackFormatSupport.build_pcm_meta(
            channels=channels,
            sampwidth=sampwidth,
            pcm_format=pcm_format,
            sample_rate=sample_rate,
        )
        if pcm_meta is None:
            self._current_file = None
            return None

        self._current_file = PlaybackFileState(
            format="pcm",
            message_id=message_id,
            channels=int(pcm_meta["channels"]),
            sample_rate=int(pcm_meta["sample_rate"]),
            sampwidth=int(pcm_meta["sampwidth"]),
            pcm_format=str(pcm_meta["pcm_format"]),
            dtype=pcm_meta["dtype"],
            scale=float(pcm_meta["scale"]),
            offset=float(pcm_meta["offset"]),
            sample_kind=str(pcm_meta["sample_kind"]),
            max_bytes=self._max_file_bytes,
        )
        return self._current_file

    def feed_binary(self, payload: bytes) -> PlaybackChunkResult:
        current = self._current_file
        if current is None or not payload:
            return PlaybackChunkResult()

        if current.format == "wav":
            next_size = current.bytes_received + len(payload)
            if next_size > current.max_bytes:
                current.dropped = True
                current.drop_reason = "size_limit"
                return PlaybackChunkResult(message_id=current.message_id, dropped=True)
            current.buffer.extend(payload)
            current.bytes_received = next_size
            return PlaybackChunkResult(message_id=current.message_id)

        if len(payload) > current.max_bytes:
            current.dropped = True
            current.drop_reason = "size_limit"
            return PlaybackChunkResult(message_id=current.message_id, dropped=True)

        arr = PlaybackFormatSupport.decode_pcm_bytes(
            payload,
            dtype=current.dtype,
            channels=current.channels,
            offset=current.offset,
            scale=current.scale,
            sample_kind=current.sample_kind,
        )
        if arr is None:
            return PlaybackChunkResult(message_id=current.message_id)
        if current.sample_rate != self._target_sample_rate:
            arr = PlaybackFormatSupport.resample(
                arr,
                float(current.sample_rate),
                float(self._target_sample_rate),
            )
        return PlaybackChunkResult(audio=arr, message_id=current.message_id)

    def finish_file(self) -> PlaybackChunkResult:
        current = self._current_file
        self._current_file = None
        if current is None:
            return PlaybackChunkResult()
        if current.dropped:
            return PlaybackChunkResult(
                message_id=current.message_id,
                dropped=True,
            )
        if current.format != "wav":
            return PlaybackChunkResult(message_id=current.message_id)
        decoded = PlaybackFormatSupport.decode_wav_bytes(bytes(current.buffer))
        if decoded is None:
            return PlaybackChunkResult(message_id=current.message_id, dropped=True)
        arr, wav_meta = decoded
        if int(wav_meta["sample_rate"]) != self._target_sample_rate:
            arr = PlaybackFormatSupport.resample(
                arr,
                float(wav_meta["sample_rate"]),
                float(self._target_sample_rate),
            )
        return PlaybackChunkResult(audio=arr, message_id=current.message_id)

    @staticmethod
    def _extract_message_id(
        metadata: Dict[str, Any], file_field: Optional[Any]
    ) -> str:
        message_id = metadata.get("message_id") or metadata.get("utterance_id")
        if not message_id and isinstance(file_field, dict):
            message_id = (
                file_field.get("file")
                or file_field.get("path")
                or file_field.get("name")
            )
        elif not message_id and isinstance(file_field, str):
            message_id = file_field
        if not message_id:
            turn_id = metadata.get("turn_id") or "turn"
            idx = metadata.get("utterance_index")
            message_id = f"{turn_id}:{idx if idx is not None else '0'}"
        return str(message_id)
