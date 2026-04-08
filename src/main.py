import asyncio

from bootstrap.runtime import create_client_runtime


async def main():
    runtime = create_client_runtime(sample_rate=16000, channels=1, blocksize=1024)
    session, ws_client = runtime.create_session()

    try:
        await session.run_once(timeout=5, playback_timeout=30.0)
    finally:
        await ws_client.close(reason="main_shutdown", trigger="main")
        runtime.speaker_adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
