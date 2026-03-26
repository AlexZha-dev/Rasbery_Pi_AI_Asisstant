# Raspberry Pi Audio Console (Mic -> WebSocket -> Speaker)

This project streams microphone audio to a WebSocket backend and plays server responses through a speaker.

It provides:
- An interactive console UI for device selection and session control
- A single-run script for quick end-to-end checks
- A local echo WebSocket server for development and testing

## Features
- Real-time microphone capture and speaker playback
- Binary and JSON WebSocket protocol support
- Session runner for non-blocking console interaction
- Local hardware integration for LCD and GPIO button (optional)
- Safer defaults for transport and payload handling

## Requirements
- Python 3.10+ (tested with Python 3.12)
- System libraries: PortAudio and libsndfile

For Debian/Ubuntu/Raspberry Pi OS:
```bash
sudo apt-get install -y libportaudio2 libsndfile1
```

Development headers (if needed):
```bash
sudo apt-get install -y python3-dev portaudio19-dev libsndfile1-dev
```

## Installation
```bash
python -m venv venv
source venv/bin/activate
make install
```

`make install` installs:
- Runtime dependencies from `requirements.txt`
- Dev/test tooling from `requirements-dev.txt`

## Configuration
Set `AUDIO_WS_URL` via environment variable or a `.env` file in the project root:

```env
AUDIO_WS_URL=wss://your-server.example/ws/audio
```

Security rules:
- The app fails fast if `AUDIO_WS_URL` is missing
- `ws://` is accepted only for localhost (`127.0.0.1`, `localhost`, `::1`)
- Remote targets must use `wss://`

## Quick Start
Run local echo server:
```bash
AUDIO_WS_URL=ws://127.0.0.1:8765 python src/tests/websocket_server.py
```

In another terminal, start the console:
```bash
python src/console_main.py
```

Console keys:
- `1` previous tab
- `3` next tab
- `2` accept/toggle action
- `q` quit

On `Microphone` and `Speaker` tabs, press `2` and enter a device index.

Single-run session example:
```bash
python src/main.py
```

## Development
Common commands:
- `make format` - run `isort` and `black`
- `make lint` - run `flake8`, `isort --check-only`, and `black --check`
- `make test` - run pytest
- `make precommit` - run all pre-commit hooks

Optional:
```bash
pre-commit install
```

## Project Layout
- `src/application/` - session orchestration (`audio_session.py`, `session_runner.py`)
- `src/infrastructure/` - hardware/network adapters (mic, speaker, websocket, lcd, button)
- `src/controllers/` - console workflow coordination
- `src/ui/` - terminal and LCD presentation layer
- `src/dto/` - message/data codecs
- `src/config/` - environment and preferences configuration
- `src/exceptions/` - domain-specific exceptions
- `src/tests/` - integration helpers, local server, and automated tests

Entry points:
- `src/console_main.py` - interactive console mode
- `src/main.py` - single session mode

## Troubleshooting
- `AUDIO_WS_URL is not defined`
  - Set it in your shell or create a `.env` file in the project root
- Cannot connect to remote `ws://` endpoint
  - Use `wss://` for non-localhost hosts
- PortAudio or ALSA errors
  - Verify system packages are installed and pick valid input/output device indices
