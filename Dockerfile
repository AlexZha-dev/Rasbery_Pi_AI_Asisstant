FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AUDIO_WS_URL=wss://your-server.example/ws/audio \
    AUDIO_WS_PASSWORD=

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libasound2 \
        libportaudio2 \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.txt

RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

CMD ["python", "src/console_main.py"]
