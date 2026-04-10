from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from infrastructure import websocket_client as ws_module
from infrastructure.websocket_client import AudioWebSocketClient
from dto.audio_message import AudioMessage


class DummyWebSocket:
    request_headers = {}
    response_headers = {}
    subprotocol = None
    close_code = 1000
    close_reason = "normal"
    close_rcvd = False

    async def close(self):
        return None


class IterableWebSocket(DummyWebSocket):
    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def make_invalid_status(status_code: int) -> InvalidStatus:
    return InvalidStatus(
        Response(status_code=status_code, reason_phrase="Forbidden", headers=Headers())
    )


@pytest.mark.asyncio
async def test_wait_for_ready_raises_timeout():
    client = AudioWebSocketClient(
        url="ws://127.0.0.1:8765", ready_timeout=0.01, max_retries=1
    )
    with pytest.raises(TimeoutError):
        await client._wait_for_ready()


@pytest.mark.asyncio
async def test_connect_respects_retry_limit(monkeypatch):
    async def always_fail(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(ws_module.websockets, "connect", always_fail)
    client = AudioWebSocketClient(
        url="ws://127.0.0.1:8765",
        max_retries=2,
        retry_backoff_base=0.001,
        retry_backoff_max=0.001,
    )
    with pytest.raises(ConnectionError):
        await client.connect()


@pytest.mark.asyncio
async def test_connect_retries_root_url_as_binary_endpoint_on_403(monkeypatch):
    calls = []

    async def fake_connect(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise make_invalid_status(403)
        return DummyWebSocket()

    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)
    client = AudioWebSocketClient(
        url="ws://127.0.0.1:8000",
        max_retries=1,
    )
    client.configure_stream(
        sample_rate=16000,
        chunk_frames=512,
        channels=1,
        sampwidth=2,
    )
    client._receiver_loop = lambda: ws_module.asyncio.sleep(0)

    await client.connect()

    assert calls[0] == "ws://127.0.0.1:8000"
    parsed = urlsplit(calls[1])
    assert parsed.path == "/ws/audio"
    assert parse_qs(parsed.query) == {
        "sample_rate": ["16000"],
        "chunk_size": ["512"],
        "channels": ["1"],
        "bit_depth_bytes": ["2"],
    }
    assert client._mode == "binary"

    await client.close(reason="test_close", trigger="pytest")


@pytest.mark.asyncio
async def test_connect_adds_handshake_query_params_for_binary_endpoint(monkeypatch):
    calls = []

    async def fake_connect(url, **kwargs):
        calls.append(url)
        return DummyWebSocket()

    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)
    client = AudioWebSocketClient(
        url="ws://127.0.0.1:8000/ws/audio",
        max_retries=1,
    )
    client.configure_stream(
        sample_rate=22050,
        chunk_frames=256,
        channels=2,
        sampwidth=2,
    )
    client._receiver_loop = lambda: ws_module.asyncio.sleep(0)

    await client.connect()

    parsed = urlsplit(calls[0])
    assert parsed.path == "/ws/audio"
    assert parse_qs(parsed.query) == {
        "sample_rate": ["22050"],
        "chunk_size": ["256"],
        "channels": ["2"],
        "bit_depth_bytes": ["2"],
    }

    await client.close(reason="test_close", trigger="pytest")


def test_heartbeat_message_detection_accepts_type_and_event_formats():
    assert AudioWebSocketClient._is_heartbeat_message(
        AudioMessage(type="heartbeat", session_id=None)
    )
    assert AudioWebSocketClient._is_heartbeat_message(
        AudioMessage(type=None, session_id=None, extra={"event": "heartbeat"})
    )
    assert not AudioWebSocketClient._is_heartbeat_message(
        AudioMessage(type="response.end", session_id=None)
    )


@pytest.mark.asyncio
async def test_receiver_loop_forwards_heartbeat_to_message_handler():
    received = []

    async def on_receive(msg):
        received.append(msg)

    client = AudioWebSocketClient(url="ws://127.0.0.1:8765", on_receive=on_receive)
    client._ws = IterableWebSocket(
        ['{"type":"heartbeat","event":"heartbeat","session_id":"test-session"}']
    )

    await client._receiver_loop()

    assert len(received) == 1
    assert received[0].type == "heartbeat"
    assert received[0].session_id == "test-session"
