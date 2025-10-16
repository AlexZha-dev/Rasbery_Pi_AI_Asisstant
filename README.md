# Raspberry Pi Audio Console (Mic → WebSocket → Speaker)

A small Python project that streams microphone audio to a WebSocket server and plays streamed audio back through the speaker. It includes:
- An interactive console UI for selecting input/output devices and toggling a recording session.
- A single-shot script to run one session end-to-end.
- A local echo WebSocket server for offline development and tests.

## Requirements
- Python 3.10+ (tested with 3.12)
- System libs: PortAudio and libsndfile
  - Debian/Ubuntu/Raspberry Pi OS: `sudo apt-get install -y libportaudio2 libsndfile1` (dev headers: `python3-dev portaudio19-dev libsndfile1-dev`)

## Installation
```bash
python -m venv venv && source venv/bin/activate
make install
```

## Configuration
Set the WebSocket URL via environment or a `.env` file in the project root:
```
AUDIO_WS_URL=ws://127.0.0.1:8765
```
Note: The app fails fast if `AUDIO_WS_URL` is missing.

## Quick Start
Run the included local echo server (persists WAV under `src/tests/outputs/`):
```bash
AUDIO_WS_URL=ws://127.0.0.1:8765 python src/tests/websocket_server.py
```
In another terminal, start the console UI:
```bash
python src/console_main.py
```
Console keys: `1` previous tab, `3` next tab, `2` accept/toggle, `q` quit. In Microphone/Speaker tabs, press `2` and enter a device index to select.

Single-shot example (records briefly, then plays streamed audio):
```bash
python src/main.py
```

## Development
- Format: `make format` (isort → black)
- Lint: `make lint` (flake8, isort --check-only, black --check)
- Tests: `make test` or `pytest -q`
- Pre-commit: `pre-commit install` then `make precommit`

## Project Structure
- `src/application/` — session orchestration (`audio_session.py`, `session_runner.py`)
- `src/infrastructure/` — devices/network adapters (mic, speaker, websocket, lcd)
- `src/controllers/` — console controller and flows
- `src/ui/` — console rendering
- `src/dto/` — dataclasses and codecs (audio payloads)
- `src/config/` — env/config loaders
- `src/exceptions/` — domain-specific errors
- `src/tests/` — integration helpers, echo server, and tests
- Entrypoints: `src/console_main.py` (console), `src/main.py` (single session)

## Troubleshooting
- “AUDIO_WS_URL is not defined” → create `.env` or export the variable.
- PortAudio/ALSA errors → ensure the correct devices are selected in the console, and required libs are installed. Try different indices if sample rate is unsupported.
