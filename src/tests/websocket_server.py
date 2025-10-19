"""
Тестовый WebSocket сервер для проверки AudioWebSocketClient.

Поддерживает два режима:
- JSON: принимает текстовые JSON-фреймы с base64 аудио (`audio_chunk`, `end_session`).
- Binary: ожидает стартовый JSON (`{"type":"start",...}`), затем бинарные PCM фреймы,
  завершение — `{"type":"end_of_chunks",...}`. Отправляет ответ в JSON, как и раньше.
"""

import asyncio
import base64
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import soundfile as sf
import websockets

# Сессии: session_id -> список numpy фреймов (float32, shape [N, C])
sessions: Dict[str, List[np.ndarray]] = {}
start_params: Dict[str, Dict[str, int]] = {}
playback_acks: Dict[str, List[Dict[str, str]]] = {}

# Папка для сохранения файлов
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def handle_client(ws):
    print(f"[SERVER] New connection path={getattr(ws, 'path', '')}")
    protocol_mode = "json"

    current_session: Optional[str] = None
    last_session_id: Optional[str] = None
    sample_rate = 16000
    channels = 1
    sampwidth = 2
    try:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                if protocol_mode != "binary" or not current_session:
                    print("[SERVER] Unexpected binary frame")
                    continue
                try:
                    dtype = np.dtype("<i2") if sampwidth == 2 else np.dtype("<i2")
                    arr_i = np.frombuffer(message, dtype=dtype)
                    if channels > 1:
                        frames = arr_i.reshape(-1, channels)
                    else:
                        frames = arr_i.reshape(-1, 1)
                    frames = frames.astype(np.float32) / 32767.0
                except Exception as e:
                    print(f"[SERVER] Failed to decode binary frame: {e}")
                    continue
                sessions.setdefault(current_session, []).append(frames)
                continue

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print("[SERVER] Received invalid JSON text frame")
                continue

            msg_type = data.get("type")
            session_id = data.get("session_id")

            if msg_type == "start":
                current_session = session_id
                last_session_id = current_session
                sample_rate = int(data.get("sample_rate", sample_rate))
                channels = int(data.get("channels", channels))
                sampwidth = int(data.get("sampwidth", sampwidth))
                chunk_size = int(data.get("chunk_size", channels * sampwidth))
                start_params[current_session] = {
                    "sample_rate": sample_rate,
                    "channels": channels,
                    "sampwidth": sampwidth,
                    "chunk_size": chunk_size,
                }
                sessions.setdefault(current_session, [])
                if protocol_mode != "binary":
                    protocol_mode = "binary"
                    await ws.send(json.dumps({"type": "ready"}))
                print(
                    f"[SERVER] Start session {current_session} sr={sample_rate} ch={channels} sw={sampwidth}"
                )
                continue

            if msg_type == "audio_chunk" and protocol_mode == "json":
                try:
                    dtype = data["dtype"]
                    shape = tuple(data["shape"])
                    arr = np.frombuffer(
                        base64.b64decode(data["data_b64"]), dtype=dtype
                    ).reshape(shape)
                    arr = arr.astype(np.float32)
                except Exception as e:
                    print(f"[SERVER] Failed to decode frame: {e}")
                    continue

                if session_id not in sessions:
                    sessions[session_id] = []
                sessions[session_id].append(arr)

                await ws.send(
                    json.dumps(
                        {
                            "type": "audio_chunk",
                            "session_id": session_id,
                            "data_b64": base64.b64encode(arr.tobytes()).decode("ascii"),
                            "dtype": str(arr.dtype),
                            "shape": arr.shape,
                        }
                    )
                )
                print(f"[SERVER] Received and echoed frame, session {session_id}")
                continue

            if msg_type in ("end_session", "end_of_chunks"):
                sid = session_id or current_session
                if not sid:
                    continue
                last_session_id = sid
                print(f"[SERVER] End session {sid}")

                frames = sessions.get(sid, [])
                combined = (
                    np.concatenate(frames, axis=0)
                    if frames
                    else np.zeros((0, channels))
                )
                filename = f"{sid}.wav"
                filepath = os.path.join(OUTPUT_DIR, filename)
                try:
                    if combined.size:
                        sf.write(
                            filepath, combined, samplerate=sample_rate, subtype="PCM_16"
                        )
                        print(f"[SERVER] Saved session to {filepath}")
                except Exception as e:
                    print(f"[SERVER] Failed to save WAV: {e}")

                if protocol_mode == "json":
                    for start in range(0, combined.shape[0], 1024):
                        block = combined[start : start + 1024]
                        block_b64 = base64.b64encode(block.tobytes()).decode("ascii")
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "audio_chunk",
                                    "session_id": sid,
                                    "data_b64": block_b64,
                                    "dtype": str(block.dtype),
                                    "shape": block.shape,
                                }
                            )
                        )
                    await ws.send(
                        json.dumps({"type": "end_session", "session_id": sid})
                    )
                    await ws.send(
                        json.dumps({"type": "playback_done", "session_id": sid})
                    )
                else:
                    turn_id = f"{sid}-turn"
                    utterances_total = 1 if combined.size else 0
                    await ws.send(
                        json.dumps(
                            {
                                "type": "response.start",
                                "session_id": sid,
                                "turn_id": turn_id,
                                "started_at": _now_iso(),
                                "trigger": "end_of_chunks",
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "playback.queue_status",
                                "session_id": sid,
                                "turn_id": turn_id,
                                "generated": utterances_total,
                                "completed": 0,
                                "pending": 0,
                                "queued": utterances_total,
                                "inflight": 0,
                                "generation_done": False,
                                "utterances_total": utterances_total,
                            }
                        )
                    )
                    if combined.size:
                        pcm = np.clip(combined, -1.0, 1.0)
                        pcm_i16 = (pcm * 32767.0).astype("<i2")
                        pcm_bytes = pcm_i16.tobytes()
                        chunk_bytes = 1024 * channels * 2
                        num_frames = combined.shape[0]
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "playback_file_start",
                                    "session_id": sid,
                                    "turn_id": turn_id,
                                    "utterance_id": "utt-1",
                                    "utterance_index": 0,
                                    "ordinal": 1,
                                    "utterances_total": utterances_total,
                                    "file": {
                                        "format": "pcm",
                                        "sample_rate": sample_rate,
                                        "channels": channels,
                                        "sampwidth": 2,
                                        "frame_size": channels * 2,
                                        "chunk_size": chunk_bytes,
                                        "num_bytes": len(pcm_bytes),
                                        "num_frames": num_frames,
                                        "pcm_format": "s16le",
                                    },
                                    "text": "Echo playback",
                                }
                            )
                        )
                        for start in range(0, len(pcm_bytes), chunk_bytes):
                            await ws.send(pcm_bytes[start : start + chunk_bytes])
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "playback_file_done",
                                    "session_id": sid,
                                    "turn_id": turn_id,
                                    "utterance_id": "utt-1",
                                    "utterance_index": 0,
                                    "file": {
                                        "format": "pcm",
                                        "num_bytes": len(pcm_bytes),
                                        "num_frames": num_frames,
                                    },
                                }
                            )
                        )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "playback.queue_status",
                                "session_id": sid,
                                "turn_id": turn_id,
                                "generated": utterances_total,
                                "completed": utterances_total,
                                "pending": 0,
                                "queued": 0,
                                "inflight": 0,
                                "generation_done": True,
                                "utterances_total": utterances_total,
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "response.end",
                                "session_id": sid,
                                "turn_id": turn_id,
                                "utterances_total": utterances_total,
                                "completed": True,
                                "duration_ms": 0,
                                "ended_at": _now_iso(),
                            }
                        )
                    )

                if sid in sessions:
                    del sessions[sid]
                current_session = None
                continue

            if msg_type == "playback_ack":
                sid = (
                    data.get("session_id")
                    or current_session
                    or last_session_id
                    or "unknown"
                )
                payload = {
                    "type": "playback_ack",
                    "message_id": data.get("message_id"),
                    "status": data.get("status"),
                }
                playback_acks.setdefault(sid, []).append(payload)
                print(
                    f"[SERVER] Received playback_ack sid={sid} "
                    f"message_id={payload['message_id']} status={payload['status']}"
                )
                continue

            if msg_type == "playback":
                print(
                    f"[SERVER] Received playback request "
                    f"mode={data.get('mode', 'background')}"
                )
                continue

            print(f"[SERVER] Unknown message type: {msg_type}")

    except websockets.ConnectionClosed:
        print("[SERVER] Connection closed")
    except Exception as e:
        print(f"[SERVER] Error: {e}")


async def main():
    host = "localhost"
    port = 8765
    print(f"[SERVER] Running ws://{host}:{port}")
    async with websockets.serve(handle_client, host, port, max_size=None):
        await asyncio.Future()  # keep running


if __name__ == "__main__":
    asyncio.run(main())
