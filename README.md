# Azure Voice Live SIP Gateway (Python)

This project bridges traditional SIP/RTP audio with the Azure AI Voice Live real-time conversation service so callers on any SIP endpoint can talk to an AI assistant.

## Features
- Receives SIP INVITE requests and exchanges RTP audio (μ-law/8 kHz) with callers.
- Streams audio to Azure Voice Live over secure WebSocket using the GA `azure-ai-voicelive` SDK.
- Transcodes between μ-law and PCM16 + resamples audio to 24 kHz for Voice Live.
- Supports proactive greetings, configurable models/voices, and both API key or AAD authentication.
- Flexible deployment: direct SIP for local testing or SIP server + SBC for production (see `docs/deployment-scenarios.md`).
- Optional per-call conversation recording with configurable output directory, duration cap, and disk-space safety checks.

## Requirements
- Python 3.11+
- **pjsua Python bindings** from pjproject (see [build instructions](https://github.com/pjsip/pjproject/tree/master/pjsip-apps/src/python))
- PortAudio headers (for optional microphone capture) `brew install portaudio` / `apt-get install -y portaudio19-dev`
- Azure subscription with **Voice Live** resource + API key or AAD app registration
- SIP softphone or SIP server for end-to-end call tests

## Quickstart
1. **Build and install pjsua Python bindings** (if not already available):
   ```bash
   # Clone pjproject and build with Python support
   git clone https://github.com/pjsip/pjproject.git
   cd pjproject
   ./configure && make dep && make
   cd pjsip-apps/src/python
   make
   # Add to PYTHONPATH or install into your virtualenv
   export PYTHONPATH=$PWD:$PYTHONPATH
   ```
2. Create a virtual environment and install gateway dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .[dev]
   ```
3. Copy the environment template and fill in your values:
   ```bash
   cp .env.template .env
   # edit .env with Azure + SIP settings
   ```
4. Load the environment variables in your shell before running:
   ```bash
   set -a && source .env && set +a
   ```
5. Start the gateway:
   ```bash
   make run           # uses PYTHONPATH=src under the hood
   # or
   PYTHONPATH=src python -m voicelive_sip_gateway.gateway.main
   ```
6. Point a softphone at `sip:test@127.0.0.1:5060` (local direct SIP) or register against your SIP server per `docs/deployment-scenarios.md`. The embedded `pjsua` stack binds to UDP/5060 and handles RTP/SDP automatically via pjproject's media stack.

## Configuration
`config/settings.py` loads settings from environment variables, `.env`, and optional CLI arguments.

| Variable | Purpose |
| --- | --- |
| `AZURE_VOICELIVE_ENDPOINT` | WebSocket endpoint (e.g., `wss://<resource>.cognitiveservices.azure.com/openai/realtime`) |
| `AZURE_VOICELIVE_API_KEY` | API key when using `AzureKeyCredential` |
| `VOICE_LIVE_MODEL` | Model identifier (`gpt-4o-realtime-preview`, etc.) |
| `VOICE_LIVE_VOICE` | Default Azure Neural or OpenAI voice |
| `VOICE_LIVE_INSTRUCTIONS` | System prompt for the assistant |
| `SIP_LOCAL_ADDRESS`, `SIP_VIA_ADDR`, `MEDIA_ADDRESS` | Network binding information |
| `MEDIA_PORT`, `MEDIA_PORT_COUNT` | Starting RTP port and number of sequential ports reserved for the RTP bridge |
| `REGISTER_WITH_SIP_SERVER` | `true/false`, registrar support planned (currently ignored) |
| `RECORDING_ENABLED` | `true/false` — enable per-call WAV recording (default: `false`) |
| `RECORDING_DIR` | Directory for WAV files (default: `recordings`) |
| `RECORDING_MAX_DURATION_SEC` | Maximum recording length in seconds (default: `1800` / 30 min) |
| `RECORDING_MIN_DISK_MB` | Minimum free disk space (MB) required before writing a recording (default: `500`) |

For AAD flows set `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET` or rely on managed identity.

More example topologies plus production SIP/SBC guidance live in `docs/deployment-scenarios.md`.

## Makefile Targets
- `make run` – executes gateway entrypoint
- `make lint` – runs Ruff + MyPy
- `make test` – executes pytest suite

## Project Layout
```
src/voicelive_sip_gateway/
  config/        # Pydantic settings + CLI glue
  gateway/       # Runtime entry point + lifecycle code
  logging/       # Structlog configuration helpers
  media/         # Audio bridging + μ-law transcoding utilities
  recording/     # Per-call WAV conversation recorder
  sip/           # SIP agent (pjsua-based)
  voicelive/     # Azure Voice Live SDK wrapper + event modeling
```

## Testing & Development
- `make test` runs the pytest suite.
- `make lint` runs Ruff plus MyPy.
- `python3 -m compileall src` is useful for quick syntax checks in CI.

## SIP & RTP Limitations
- Only one concurrent call is supported; additional INVITEs receive `486 Busy Here` until the active session ends.
- Media support is limited to G.711 μ-law (payload type 0); pjproject handles codecs/RTP internally.
- Registrar support (`REGISTER_WITH_SIP_SERVER=true`) is implemented via pjsua's account registration.

## Deployment Notes
- Local testing: run gateway on `127.0.0.1` and connect a SIP softphone directly.
- Production: deploy behind an SBC + SIP server, expose RTP/SIP ports, and set the SIP_* env vars (examples in `docs/deployment-scenarios.md`).
- Containerization: add Docker support by installing dependencies into an image, copying `.env` or secrets via Kubernetes/ACI, and running `python -m voicelive_sip_gateway.gateway.main`.

## Docker Quick Start

Build and run the gateway in Docker with a single command:

```powershell
docker stop voicelive-gateway 2>$null; docker rm voicelive-gateway 2>$null; docker build -t voicelive-gateway . ; docker run -d --name voicelive-gateway --env-file .env -p 5080:5080/udp -p 5080:5080/tcp -p 10000-10100:10000-10100/udp voicelive-gateway
```

This command:
1. Stops and removes any existing `voicelive-gateway` container
2. Builds a fresh image from the `Dockerfile`
3. Runs the container in detached mode with environment variables from `.env`
4. Exposes SIP on port 5080 (UDP + TCP) and RTP on ports 10000-10100 (UDP)

### Testing with MicroSIP

1. Download and install [MicroSIP](https://www.microsip.org/) (free open-source SIP softphone for Windows)
2. Configure MicroSIP to use **TCP transport** (Settings → Network → Transport: TCP)
3. Make a call to: `sip:test@127.0.0.1:5080`
4. You should hear the AI assistant greeting and can begin a conversation

> **Tip:** Use `docker logs -f voicelive-gateway` to monitor gateway logs in real-time during testing.

## Call Recording

The gateway supports optional per-call conversation recording that captures both the caller and the AI assistant into a single WAV file. Recording is disabled by default and controlled entirely via environment variables.

### Enabling Recording

Set `RECORDING_ENABLED=true` in your `.env` file (or pass it as an environment variable). Recordings are written to the `RECORDING_DIR` directory (default: `recordings/`) with filenames following the pattern:

```
call_{caller_number}_{YYYYMMDD_HHmmss}.wav
```

For example: `call_+15551234567_20260216_143022.wav`

### Technical Implementation

Recording is implemented in the `recording/` module via the `CallRecorder` class and integrates into the existing audio pipeline with zero impact on call quality.

#### Architecture

```
┌─────────────┐       ┌──────────────────────┐       ┌─────────────────┐
│  SIP Caller  │──RTP──▶  pjsua Conference    │──────▶│  VoiceLive API  │
│  (8 kHz)     │◀──RTP──  Bridge (PCM16 8kHz) │◀──────│  (24 kHz)       │
└─────────────┘       └──────────┬───────────┘       └─────────────────┘
                                 │
                        ┌────────▼────────┐
                        │  CallRecorder   │
                        │  (taps both     │
                        │   directions)   │
                        └────────┬────────┘
                                 │ finalize()
                        ┌────────▼────────┐
                        │  WAV File       │
                        │  mono 8kHz PCM16│
                        └─────────────────┘
```

#### Audio Capture Points

The recorder taps audio at two points inside `AudioStreamBridge`, both at the native 8 kHz PCM16 level — no extra resampling is performed for recording:

1. **Caller audio** — captured in `enqueue_sip_audio()` when raw PCM16 8 kHz frames arrive from pjsua (before upsampling to 24 kHz for VoiceLive). Each frame is 320 bytes (20 ms at 8 kHz, 16-bit mono).

2. **AI audio** — captured in `dequeue_sip_audio_sync()` when frames are delivered to pjsua for RTP transmission (after downsampling from 24 kHz). This includes both real audio frames and silence frames. By tapping at the dequeue point rather than the ingest point, the recorder captures **only audio that was actually played to the caller** — AI speech discarded during barge-in interruptions is excluded.

#### Timing Synchronization

Both buffers stay perfectly time-aligned because:
- The caller buffer receives one 20 ms frame per `enqueue_sip_audio()` call from the pjsua media thread.
- The AI buffer receives one 20 ms frame per `dequeue_sip_audio_sync()` call — including silence frames when no AI audio is queued. Since pjsua calls this method on a strict 20 ms cadence, both buffers grow at exactly the same real-time rate.

#### Mixing & Output

At call teardown, `CallRecorder.finalize()` runs in a thread-pool executor (off the event loop) and:
1. Converts both `bytearray` buffers to `numpy` int16 arrays.
2. Zero-pads the shorter array to match the longer one.
3. Sums both arrays with `int32` arithmetic and clips to `[-32768, 32767]` to prevent overflow.
4. Writes the result as a standard WAV file (mono, 8 kHz, 16-bit PCM) using Python's built-in `wave` module — no external audio dependencies required.

#### Thread Safety

No locks are needed. Each buffer has a single writer:
- `_caller_buf` is written exclusively from the asyncio event-loop thread (via `enqueue_sip_audio`).
- `_ai_buf` is written exclusively from the pjsua media thread (via `dequeue_sip_audio_sync`).
- `finalize()` runs only after both writers have stopped (post-call cleanup), so there is no concurrent access.

#### Production Safety

| Guard | Behavior |
|---|---|
| **Duration cap** | Recording stops accepting frames after `RECORDING_MAX_DURATION_SEC` (default 30 min). The call itself continues unaffected. Peak memory per call: ~57 MB (two 28.8 MB buffers). |
| **Disk-space check** | Before writing, `shutil.disk_usage()` verifies at least `RECORDING_MIN_DISK_MB` free space. If insufficient, the recording is skipped with an error log — the call is never interrupted. |
| **Graceful failure** | Any exception during `finalize()` is caught and logged. Recording failures never propagate to the SIP call or VoiceLive session. |
| **Memory release** | Buffers are explicitly cleared after WAV write (or on skip) to promptly free memory for the next call. |

#### Docker Volumes

When running in Docker, recordings are persisted to the host via a volume mount configured in `docker-compose.yml`:

```yaml
volumes:
  - ./recordings:/app/recordings
```

This ensures recordings survive container restarts. The `Dockerfile` creates the `/app/recordings` directory during build.
