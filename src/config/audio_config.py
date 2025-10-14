import os
from dotenv import load_dotenv

load_dotenv()

AUDIO_WS_URL = os.getenv("AUDIO_WS_URL")

if not AUDIO_WS_URL:
    raise RuntimeError(
        "AUDIO_WS_URL is not defined. Please create a .env file with AUDIO_WS_URL=wss://your-server"
    )
