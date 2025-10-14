"""
Тестовый WebSocket сервер для проверки AudioWebSocketClient.
- Принимает аудио-фреймы и сохраняет их по сессии.
- После 'end_session' объединяет все фреймы, сохраняет в WAV в outputs/ и отправляет обратно клиенту.
"""

import asyncio
import json
import base64
import os
import numpy as np
import websockets
import soundfile as sf

# Сессии: session_id -> список numpy фреймов
sessions = {}

# Папка для сохранения файлов
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)


async def handle_client(ws):
    print(f"[SERVER] New connection")
    try:
        async for message in ws:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print("[SERVER] Received invalid JSON")
                continue

            msg_type = data.get("type")
            session_id = data.get("session_id")

            if msg_type == "audio_chunk":
                try:
                    dtype = data["dtype"]
                    shape = tuple(data["shape"])
                    arr = np.frombuffer(base64.b64decode(data["data_b64"]), dtype=dtype).reshape(shape)
                except Exception as e:
                    print(f"[SERVER] Failed to decode frame: {e}")
                    continue

                if session_id not in sessions:
                    sessions[session_id] = []
                sessions[session_id].append(arr)

                # Эхо фрейм обратно
                await ws.send(json.dumps({
                    "type": "audio_chunk",
                    "session_id": session_id,
                    "data_b64": data["data_b64"],
                    "dtype": dtype,
                    "shape": shape
                }))
                print(f"[SERVER] Received and echoed frame, session {session_id}")

            elif msg_type == "end_session":
                print(f"[SERVER] End session {session_id}")

                frames = sessions.get(session_id, [])
                if frames:
                    combined = np.concatenate(frames, axis=0)
                    # Сохраняем в WAV
                    filename = f"{session_id}.wav"
                    filepath = os.path.join(OUTPUT_DIR, filename)
                    sf.write(filepath, combined, samplerate=16000, subtype="PCM_16")
                    print(f"[SERVER] Saved session to {filepath}")

                    # Отправляем файл обратно клиенту по блокам
                    blocksize = 1024
                    for start in range(0, combined.shape[0], blocksize):
                        block = combined[start:start + blocksize]
                        block_b64 = base64.b64encode(block.tobytes()).decode("ascii")
                        await ws.send(json.dumps({
                            "type": "audio_chunk",
                            "session_id": session_id,
                            "data_b64": block_b64,
                            "dtype": str(block.dtype),
                            "shape": block.shape
                        }))
                    print(f"[SERVER] Sent WAV back as frames for session {session_id}")

                # Сигнал завершения
                await ws.send(json.dumps({
                    "type": "end_session",
                    "session_id": session_id
                }))

                if session_id in sessions:
                    del sessions[session_id]

            else:
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
