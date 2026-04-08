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
- Python 3.10-3.13
- Python 3.12 recommended
- Python 3.14 is not supported yet because some dependencies may fall back to
  source builds on Windows
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

On Windows PowerShell:
```powershell
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
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
- `pydantic-core` fails with a Rust or Cargo error during install
  - Recreate the virtual environment with Python 3.12 or 3.13 and rerun
    `make install`

## Rewrite Plan
This section describes an incremental rewrite plan for `RasberyPiClient`.
The goal is to improve playback reliability, session lifecycle handling, and
device management without losing the current working flow.

### Why Rewrite
- The current client mixes audio capture, playback, transport, and UI concerns
  in a few large modules.
- Playback completion depends on timing-sensitive events and timeout handling.
- Device persistence is still vulnerable to runtime changes in hardware order,
  especially on Raspberry Pi and USB audio setups.
- Console interaction, hardware buttons, and session orchestration are coupled
  closely enough that regressions in one area can block another.
- Diagnosing "server created audio, but speaker stayed silent" is harder than
  it should be.

### Rewrite Goals
- Make session flow fully state-driven and deterministic.
- Separate audio I/O, transport, orchestration, UI, and hardware adapters.
- Make playback observable: every received chunk, stream open, drain, and stop
  condition should be traceable.
- Persist devices by stable signatures, not by fragile runtime indices alone.
- Support Raspberry Pi deployment as a first-class target, including headless
  mode and service startup.

### Non-Goals
- Do not rewrite the server protocol in phase 1 unless a client-safe adapter is
  impossible.
- Do not expand LCD or GPIO features until the core audio path is reliable.
- Do not attempt a big-bang rewrite; keep the existing entry points working
  during migration.

### Target Architecture
- `domain/`
  - Session states, domain events, audio format contracts, and device identity
    models
- `application/`
  - `SessionOrchestrator`, `RecordingCoordinator`, `PlaybackCoordinator`,
    `DiagnosticsService`
- `infrastructure/audio/`
  - `MicrophoneEngine`, `SpeakerEngine`, `DeviceResolver`, queue/buffer helpers
- `infrastructure/transport/`
  - `AudioWsClient`, protocol codec, reconnect/backoff policy
- `interfaces/`
  - `console/`, `lcd/`, `buttons/`, and optional `headless/` service mode
- `bootstrap/`
  - dependency wiring, config loading, startup health checks

### Core Design Rules
- UI never owns session logic; it only sends commands and renders state.
- Playback owns exactly one output stream and one queue per active session.
- Recording, transport, and playback communicate through explicit events rather
  than direct side effects.
- Audio format normalization happens before playback starts and is logged with
  actual sample rate, channels, and chunk sizes.
- Saved device preferences must fall back safely to system defaults when the
  original hardware is missing.
- "No sound" must be diagnosable from logs without attaching a debugger.

### Recommended Package Split From Current Code
- `src/application/audio_session.py`
  - Split into orchestration, playback completion tracking, and server event
    handling.
- `src/application/session_runner.py`
  - Keep as a thin async host around the orchestrator, not as a logic owner.
- `src/infrastructure/speaker_output.py`
  - Replace with a dedicated speaker engine that owns one long-lived output
    stream and explicit drain behavior.
- `src/infrastructure/microphone_interface.py`
  - Move capture concerns into a microphone engine with clear start/stop/read
    contracts.
- `src/infrastructure/websocket_client.py`
  - Split protocol framing, retry policy, and diagnostics logging.
- `src/controllers/console_controller.py`
  - Reduce to command routing plus view refresh; no session lifecycle decisions.

### Phased Migration Plan
#### Phase 0 - Stabilize the Current Client
- Freeze the current protocol and record the expected message flow.
- Expand tests around playback completion, device resolution, and console
  return-to-idle behavior.
- Add runtime diagnostics for selected devices, stream parameters, and playback
  queue activity.
- Define a short manual test checklist for Raspberry Pi hardware.

#### Phase 1 - Define Stable Contracts
- Introduce explicit session states such as `idle`, `recording`,
  `waiting_response`, `playing`, `stopping`, `error`, and `completed`.
- Define typed events for microphone frames, transport events, playback start,
  playback stop, and fatal errors.
- Create a stable device identity model using device name, host API, and
  channel capabilities.
- Add audio format objects so sample rate, channels, dtype, and frame size are
  validated in one place.

#### Phase 2 - Replace Audio I/O
- Build `MicrophoneEngine` with explicit `start`, `read`, `stop`, and `close`
  semantics.
- Build `SpeakerEngine` with one owned output stream, a playback queue, and
  `flush`/`drain` support.
- Normalize mono/stereo routing inside the engine, not in session code.
- Add a startup self-check that can play a short test tone and report the
  actual output device in use.

#### Phase 3 - Replace Session Orchestration
- Build `SessionOrchestrator` as the single owner of session lifecycle.
- Move timeout logic, end-of-session rules, and playback completion rules into
  the orchestrator state machine.
- Make transport input and playback output event-driven and fully testable.
- Keep the old console entry point, but swap its internals to the new
  orchestrator behind a compatibility adapter.

#### Phase 4 - Rebuild UI and Hardware Integration
- Keep the console non-blocking and driven by observed runner state.
- Move LCD rendering and button handling behind small adapter interfaces.
- Add headless mode for `systemd` or kiosk-style startup on Raspberry Pi.
- Ensure UI failures cannot leave audio resources open.

#### Phase 5 - Deployment, Diagnostics, and Cutover
- Add structured logs for session state, selected devices, playback progress,
  and websocket close reasons.
- Add a hardware validation script for microphone capture, websocket reachability,
  and speaker playback.
- Ship a `systemd` service template and startup troubleshooting notes for
  Raspberry Pi OS.
- Run the new client side-by-side with the old one until parity is confirmed.

### Testing Strategy
- Unit tests for device resolution, audio normalization, and state transitions.
- Integration tests for websocket roundtrip, playback completion, and console
  control flow.
- Hardware smoke tests for default devices, USB headset changes, and unplug/replug
  recovery.
- Failure-path tests for server silence, malformed playback payloads, and forced
  websocket disconnects.

### Definition of Done
- The client can prove, via logs, whether audio was captured, sent, received,
  decoded, queued, and handed to the output device.
- Ending a session always returns the UI to a usable idle state.
- Device selection survives reboots and missing hardware gracefully.
- Raspberry Pi can run the client both interactively and as a background
  service.
- Playback reliability is verified on at least one desktop environment and one
  Raspberry Pi audio setup.
