"""SIP agent powered by pjsua with custom media bridge to Voice Live."""
from __future__ import annotations

import asyncio
import base64
import threading
from typing import Optional
import traceback
import structlog
from structlog.stdlib import BoundLogger

try:
    import pjsua2 as pj
except ImportError:
    raise ImportError(
        "pjsua2 module not found. Build and install pjproject with Python bindings.\n"
        "See: https://github.com/pjsip/pjproject/tree/master/pjsip-apps/src/swig"
    )

from voicelive_sip_gateway.config.settings import Settings
from voicelive_sip_gateway.media.stream_bridge import AudioStreamBridge
from voicelive_sip_gateway.voicelive.client import VoiceLiveClient
from voicelive_sip_gateway.voicelive.events import VoiceLiveEventType


class CustomAudioMediaPort(pj.AudioMediaPort):
    """Custom audio port that bridges pjsua media with Voice Live."""

    def __init__(self, bridge: AudioStreamBridge, direction: str, logger: BoundLogger):
        super().__init__()
        self._bridge = bridge
        self._direction = direction  # "to_voicelive" or "from_voicelive"
        self._logger = logger
        self._loop = None
        
        # Create the port with appropriate settings
        fmt = pj.MediaFormatAudio()
        fmt.type = pj.PJMEDIA_TYPE_AUDIO
        fmt.clockRate = 8000  # SIP μ-law is 8kHz
        fmt.channelCount = 1
        fmt.bitsPerSample = 16
        fmt.frameTimeUsec = 20000  # 20ms frames
        
        port_name = f"voicelive_{direction}"
        self.createPort(port_name, fmt)
        
    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def onFrameRequested(self, frame: pj.MediaFrame) -> None:
        """Called by pjsua when it needs audio to send to caller (from Voice Live)."""
        if self._direction == "from_voicelive" and self._loop:
            try:
                # Try to get audio without blocking - use non-blocking method
                # The dequeue_sip_audio_nonblocking method returns silence if queue is empty
                future = asyncio.run_coroutine_threadsafe(
                    self._bridge.dequeue_sip_audio_nonblocking(),
                    self._loop
                )
                # Use a longer timeout for the future itself to avoid TimeoutError
                pcm_data = future.result(timeout=0.050)  # 50ms timeout for safety
                
                # For 8kHz audio with 20ms frames: 160 samples * 2 bytes = 320 bytes
                expected_bytes = 320
                if len(pcm_data) != expected_bytes:
                    # Pad or truncate to expected size
                    if len(pcm_data) < expected_bytes:
                        pcm_data = pcm_data + b'\x00' * (expected_bytes - len(pcm_data))
                    else:
                        pcm_data = pcm_data[:expected_bytes]
                
                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.buf = pj.ByteVector(pcm_data)
                frame.size = len(pcm_data)
            except Exception as e:
                # Log only if it's not a routine timeout
                if not isinstance(e, TimeoutError):
                    self._logger.debug("media.frame_error", error=str(e), error_type=type(e).__name__)
                # Return silence frame
                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.buf = pj.ByteVector(b'\x00' * 320)
                frame.size = 320

    def onFrameReceived(self, frame: pj.MediaFrame) -> None:
        """Called by pjsua when it receives audio from caller (to Voice Live)."""
        if self._direction == "to_voicelive" and self._loop:
            if frame.type == pj.PJMEDIA_FRAME_TYPE_AUDIO and frame.buf:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._bridge.enqueue_sip_audio(bytes(frame.buf)),
                        self._loop
                    )
                except Exception as e:
                    self._logger.warning("media.enqueue_failed", error=str(e))


class GatewayCall(pj.Call):
    """Handles SIP call lifecycle and connects media bridge."""

    def __init__(self, account, call_id: int, logger: BoundLogger, bridge: AudioStreamBridge, 
                 voicelive_client: VoiceLiveClient, loop: asyncio.AbstractEventLoop):
        super().__init__(account, call_id)
        self._account = account
        self._logger = logger
        self._bridge = bridge
        self._voicelive_client = voicelive_client
        self._loop = loop
        self._to_voicelive_port = None
        self._from_voicelive_port = None
        self._event_task = None

    def onCallState(self, prm: pj.OnCallStateParam) -> None:
        ci = self.getInfo()
        self._logger.info(
            "sip.call_state",
            remote_uri=ci.remoteUri,
            state=ci.stateText,
            code=ci.lastStatusCode,
        )
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self._cleanup()
            self._account.current_call = None

    def onCallMediaState(self, prm: pj.OnCallMediaStateParam) -> None:
        ci = self.getInfo()
        for mi in ci.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                media = self.getMedia(mi.index)
                aud_media = pj.AudioMedia.typecastFromMedia(media)
                
                # Create custom media ports to bridge pjsua ↔ Voice Live
                self._to_voicelive_port = CustomAudioMediaPort(self._bridge, "to_voicelive", self._logger)
                self._from_voicelive_port = CustomAudioMediaPort(self._bridge, "from_voicelive", self._logger)
                
                self._to_voicelive_port.set_event_loop(self._loop)
                self._from_voicelive_port.set_event_loop(self._loop)
                
                # Connect bidirectional audio flow
                # Caller audio -> to_voicelive_port -> Voice Live
                aud_media.startTransmit(self._to_voicelive_port)
                # Voice Live -> from_voicelive_port -> Caller audio
                self._from_voicelive_port.startTransmit(aud_media)
                
                # Start processing Voice Live events
                self._event_task = asyncio.run_coroutine_threadsafe(
                    self._process_voicelive_events(),
                    self._loop
                )
                
                # Start the conversation with a greeting
                asyncio.run_coroutine_threadsafe(
                    self._voicelive_client.request_response(interrupt=False),
                    self._loop
                )
                
                self._logger.info("sip.media_active", message="Voice Live bridge established")
                break
        else:
            self._logger.info("sip.media_inactive")

    async def _process_voicelive_events(self) -> None:
        """Process Voice Live events and route audio to SIP."""
        try:
            async for event in self._voicelive_client.events():
                if event.type == VoiceLiveEventType.RESPONSE_AUDIO_DELTA:
                    audio_bytes = event.payload["_data"]["delta"]
                    if audio_bytes:
                        try:
                            # Decode base64 to get raw PCM16 24kHz bytes
                            pcm_data = base64.b64decode(audio_bytes)
                            self._logger.debug(
                                "voicelive.audio_delta",
                                chunk_bytes=len(pcm_data),
                                chunk_samples=len(pcm_data) // 2,
                            )
                            # PCM16 24kHz from Voice Live -> convert to μ-law 8kHz for SIP
                            await self._bridge.emit_audio_to_sip(pcm_data)
                        except Exception as decode_err:
                            self._logger.warning("voicelive.audio_decode_failed", error=str(decode_err))
                elif event.type == VoiceLiveEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                    # User started speaking - interrupt the bot
                    self._logger.info("voicelive.user_speech_started", message="Interrupting bot response")
                    # Clear any pending audio in the outbound queue
                    self._bridge.clear_outbound_queue()
                    # Send interrupt signal to Voice Live to stop generating audio
                    #await self._voicelive_client.request_response(interrupt=True)
                elif event.type == VoiceLiveEventType.ERROR:
                    error_msg = event.payload.get("error", str(event.payload))
                    self._logger.error("voicelive.event_error", error=error_msg)
                    raise RuntimeError(f"Voice Live error: {error_msg}")
                elif event.type == VoiceLiveEventType.INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                    self._logger.info("voicelive.transcription_completed", details=event.payload["_data"]["transcript"])
                elif event.type == VoiceLiveEventType.RESPONSE_OUTPUT_ITEM_DONE:
                    self._logger.info("voicelive.response_output_item_done", details= event.payload["_data"])
                else:
                    self._logger.info("voicelive.event_received", event_type=event)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._logger.error("voicelive.event_processing_error", error=str(e))

    def _cleanup(self) -> None:
        """Clean up media ports and tasks."""
        if self._event_task:
            self._event_task.cancel()
            self._event_task = None
        if self._to_voicelive_port:
            self._to_voicelive_port = None
        if self._from_voicelive_port:
            self._from_voicelive_port = None


class GatewayAccount(pj.Account):
    """Account that limits to one concurrent call."""

    def __init__(self, logger: BoundLogger, bridge: AudioStreamBridge, 
                 voicelive_client: VoiceLiveClient, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._logger = logger
        self._bridge = bridge
        self._voicelive_client = voicelive_client
        self._loop = loop
        self.current_call: Optional[GatewayCall] = None

    def onIncomingCall(self, prm: pj.OnIncomingCallParam) -> None:
        if self.current_call:
            call = GatewayCall(self, prm.callId, self._logger, self._bridge, 
                              self._voicelive_client, self._loop)
            ci = call.getInfo()
            self._logger.warning("sip.busy", remote_uri=ci.remoteUri)
            call.hangup(pj.CallOpParam())
            return

        self._logger.info("sip.incoming_call")
        call = GatewayCall(self, prm.callId, self._logger, self._bridge, 
                          self._voicelive_client, self._loop)
        self.current_call = call
        
        # Send 180 Ringing first
        ringing_param = pj.CallOpParam()
        ringing_param.statusCode = 180
        call.answer(ringing_param)
        
        # Then accept the call with 200 OK
        accept_param = pj.CallOpParam()
        accept_param.statusCode = 200
        call.answer(accept_param)


class SipAgent:
    """Coordinates pjsua signaling with RTP/Voice Live bridging."""

    def __init__(
        self,
        settings: Settings,
        bridge: AudioStreamBridge,
        voicelive_client: VoiceLiveClient,
    ) -> None:
        self._settings = settings
        self._bridge = bridge
        self._voicelive_client = voicelive_client
        self._logger: BoundLogger = structlog.get_logger(__name__)
        self._ep: Optional[pj.Endpoint] = None
        self._transport: Optional[pj.TransportConfig] = None
        self._account: Optional[GatewayAccount] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return

        await self._bridge.attach_voicelive_client(self._voicelive_client)

        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run_pjsua_thread, args=(loop,), daemon=True)
        self._thread.start()
        self._running = True
        self._logger.info(
            "sip.agent_started",
            address=self._settings.sip.local_address,
            port=self._settings.sip.port,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._ep:
            try:
                if self._account:
                    self._account.shutdown()
                    del self._account
                    self._account = None
                self._ep.libDestroy()
                del self._ep
                self._ep = None
            except Exception as exc:
                self._logger.warning("sip.stop_error", error=str(exc))
        await self._bridge.close()
        self._logger.info("sip.agent_stopped")

    def _run_pjsua_thread(self, loop: asyncio.AbstractEventLoop) -> None:
        """Runs pjsua2 event loop in a dedicated thread."""
        try:
            self._ep = pj.Endpoint()
            self._ep.libCreate()

            ep_cfg = pj.EpConfig()
            # Lower log level to hide pjmedia playdbuf underflow warnings.
            ep_cfg.logConfig.level = 2
            ep_cfg.logConfig.consoleLevel = 2
            self._ep.libInit(ep_cfg)

            transport_cfg = pj.TransportConfig()
            transport_cfg.port = self._settings.sip.port
            self._ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, transport_cfg)
            
            self._ep.libStart()
            self._logger.info(
                "sip.transport_created",
                port=self._settings.sip.port,
            )

            self._account = GatewayAccount(self._logger, self._bridge, 
                                          self._voicelive_client, loop)
            
            if self._settings.sip.register_with_server and self._settings.sip.server:
                acc_cfg = pj.AccountConfig()
                acc_cfg.idUri = f"sip:{self._settings.sip.user}@{self._settings.sip.server}"
                acc_cfg.regConfig.registrarUri = f"sip:{self._settings.sip.server}"
                
                cred = pj.AuthCredInfo()
                cred.scheme = "digest"
                cred.realm = self._settings.sip.auth_realm or "*"
                cred.username = self._settings.sip.auth_user or self._settings.sip.user or ""
                cred.data = self._settings.sip.auth_password or ""
                cred.dataType = pj.PJSIP_CRED_DATA_PLAIN_PASSWD
                acc_cfg.sipConfig.authCreds.append(cred)
                
                self._account.create(acc_cfg)
            else:
                acc_cfg = pj.AccountConfig()
                acc_cfg.idUri = f"sip:{self._settings.sip.user or 'gateway'}@{self._settings.sip.local_address}:{self._settings.sip.port}"
                self._account.create(acc_cfg)

            while self._running:
                self._ep.libHandleEvents(100)

        except Exception as exc:
            self._logger.error("sip.thread_error", error=str(exc), type=type(exc).__name__)
