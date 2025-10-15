import asyncio
import threading
import time

import pytest

from application.session_runner import SessionRunner


async def async_eventually(condition, *, timeout: float, stage: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"Stage '{stage}' timed out after {timeout}s")


class QuickSession:
    async def run_once(
        self, timeout: float, playback_timeout: float
    ) -> None:  # pragma: no cover - simple coroutine
        await asyncio.sleep(0.05)


class BlockingSession:
    def __init__(self, stop_event: asyncio.Event):
        self._stop_event = stop_event

    async def run_once(self, timeout: float, playback_timeout: float) -> None:
        await self._stop_event.wait()


class DummyClient:
    def __init__(self, closed_event: asyncio.Event):
        self._closed_event = closed_event

    async def close(self) -> None:
        self._closed_event.set()


@pytest.mark.asyncio
async def test_session_runner_completes_and_closes_client():
    closed_event = asyncio.Event()

    def factory():
        return QuickSession(), DummyClient(closed_event)

    runner = SessionRunner(factory, request_stop=lambda: None)
    ok, msg = await runner.start()
    assert ok, msg
    await async_eventually(
        lambda: not runner.is_running(), timeout=2.0, stage="session finish"
    )
    status = runner.get_status()
    assert status.state == "idle"
    assert "completed" in status.message.lower()
    await asyncio.wait_for(closed_event.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_session_runner_stop_triggers_callback():
    stop_event = asyncio.Event()
    closed_event = asyncio.Event()
    stop_called = threading.Event()
    loop = asyncio.get_running_loop()

    def factory():
        return BlockingSession(stop_event), DummyClient(closed_event)

    def request_stop():
        stop_called.set()
        loop.call_soon_threadsafe(stop_event.set)

    runner = SessionRunner(factory, request_stop=request_stop)
    await runner.start()
    await async_eventually(
        lambda: runner.get_status().state == "recording",
        timeout=2.0,
        stage="runner start",
    )
    ok, msg = await runner.stop()
    assert ok, msg
    await async_eventually(stop_called.is_set, timeout=1.0, stage="stop callback")
    await async_eventually(
        lambda: runner.get_status().state == "idle", timeout=3.0, stage="runner idle"
    )
    await asyncio.wait_for(closed_event.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_session_runner_stop_before_execute():
    run_called = asyncio.Event()
    close_event = asyncio.Event()
    stop_called = threading.Event()

    class SessionSpy:
        async def run_once(self, timeout: float, playback_timeout: float) -> None:
            run_called.set()
            await asyncio.sleep(0)

    class ClientSpy:
        async def close(self) -> None:
            close_event.set()

    def factory():
        return SessionSpy(), ClientSpy()

    def request_stop():
        stop_called.set()

    runner = SessionRunner(
        factory,
        request_stop=request_stop,
        session_timeout=0.5,
        playback_timeout=0.5,
        join_timeout=0.5,
    )

    ok, msg = await runner.start()
    assert ok, msg

    ok, msg = await runner.stop()
    assert ok, msg
    assert stop_called.is_set()
    assert not run_called.is_set()
    await asyncio.wait_for(close_event.wait(), timeout=1.0)
    status = runner.get_status()
    assert status.state == "idle"
    assert "stopped" in status.message.lower()
