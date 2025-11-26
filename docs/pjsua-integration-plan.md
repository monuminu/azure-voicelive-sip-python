# PJSUA Integration Plan

## Goals
- Replace the simulated SIP agent with a production-ready implementation powered by PJSIP's `pjsua` Python bindings.
- Preserve the existing Voice Live media bridge so audio continues to flow between RTP callers (μ-law/8 kHz) and Azure Voice Live (PCM16/24 kHz).
- Maintain drop-in compatibility with the current configuration model (`.env` + `Settings`) so local and hosted deployments follow the same workflow described in `README.md`.

## Architecture Overview
1. **Library bootstrap**
   - Install `pjsua` (requires native PJSIP libs, PortAudio, OpenSSL).
   - Initialize `pj.Lib` with structured logging callback, disable sound device, and rely on the internal conference bridge for routing.
   - Create a UDP transport bound to `SIP_LOCAL_ADDRESS:SIP_PORT`. TLS support remains future work.
2. **Account provisioning**
   - For direct SIP (default), create a local account with URI `sip:{SIP_USER or "test"}@{SIP_LOCAL_ADDRESS}` with registration disabled.
   - If `REGISTER_WITH_SIP_SERVER=true`, configure the registrar, credentials, and optional outbound proxy derived from `SIP_SERVER` + auth fields.
3. **Call handling**
   - Subclass `pj.AccountCallback` to accept inbound calls and attach a `CallCallback`.
   - `CallCallback.on_state()` tracks ringing/connected/disconnected transitions and triggers proactive Voice Live greeting.
   - `CallCallback.on_media_state()` attaches media streams once `MediaState.ACTIVE`.
4. **Media bridge**
   - Build two custom `pj.MediaPort` adapters:
     - **SipToVoiceLivePort** (implements `put_frame`), receives PCM16 audio from pjsua, converts to μ-law, enqueues via `AudioStreamBridge.enqueue_sip_audio`.
     - **VoiceLiveToSipPort** (implements `get_frame`), pulls μ-law from `AudioStreamBridge.dequeue_sip_audio`, converts to PCM16 for PJSIP.
   - Connect these ports to the call's `AudioMedia` using the pjsua conference bridge (`lib.conf_connect`).
   - Reuse the existing transcoding helpers (`mulaw_to_pcm16`, `pcm16_to_mulaw`, `resample_pcm16`).
5. **Shutdown**
   - `SipAgent.stop()` gracefully hangs up calls, destroys accounts/transports, and terminates `pj.Lib`.
   - Background asyncio tasks are cancelled to avoid dangling references.

## Error Handling & Telemetry
- Emit structlog events for major transitions (`sip.transport_ready`, `sip.invite_received`, `sip.media_started`, `sip.call_disconnected`).
- Map PJSIP status codes/exceptions to human-readable errors when startup fails (e.g., port already bound).
- Surface media health (packet underrun/overrun) via throttled warnings from the custom media ports.

## Work Items
1. Add `pjsua` dependency and document native setup commands for macOS in `README.md`.
2. Replace `SipAgent` stub with the implementation described above (`sip/agent.py`).
3. Create `sip/media_ports.py` (or similar) to host reusable adapters between pjsua frames and `AudioStreamBridge` queues.
4. Extend settings if additional SIP knobs are required (transport type, RTP port range).
5. Update docs + `.env.template` to highlight new requirements.
6. Add a smoke test or health check that verifies the SIP transport binds successfully when running under CI (skipped if `pjsua` missing).

## Open Questions
- TLS transport: defer until after UDP flow is validated.
- Multi-call management: initial scope supports a single concurrent call; future enhancement may queue additional INVITEs.
- External SIP registrar keep-alive intervals: rely on `pjsua` defaults initially, but expose overrides if needed.
