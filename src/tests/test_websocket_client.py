import pytest

from infrastructure import websocket_client as ws_module
from infrastructure.websocket_client import AudioWebSocketClient


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
