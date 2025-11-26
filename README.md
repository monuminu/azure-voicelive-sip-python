# Azure Voice Live SIP Gateway (Python)

This project bridges traditional SIP/RTP audio with the Azure AI Voice Live real-time conversation service so callers on any SIP endpoint can talk to an AI assistant.

## Features
- Receives SIP INVITE requests and exchanges RTP audio (μ-law/8 kHz) with callers.
- Streams audio to Azure Voice Live over secure WebSocket using the GA `azure-ai-voicelive` SDK.
- Transcodes between μ-law and PCM16 + resamples audio to 24 kHz for Voice Live.
- Supports proactive greetings, configurable models/voices, and both API key or AAD authentication.
- Flexible deployment: direct SIP for local testing or SIP server + SBC for production (see `docs/deployment-scenarios.md`).

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
