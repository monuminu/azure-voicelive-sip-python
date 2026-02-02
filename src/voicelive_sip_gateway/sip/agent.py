"""SIP agent powered by pjsua with custom media bridge to Voice Live."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import threading
import traceback
from typing import Optional

import structlog
from structlog.stdlib import BoundLogger

# Azure VoiceLive SDK imports
from azure.ai.voicelive.models import (
    ServerEventType,
    ServerEventConversationItemCreated,
    ResponseFunctionCallItem,
)

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


class CustomAudioMediaPort(pj.AudioMediaPort):
    """Custom audio port that bridges pjsua media with Voice Live."""

    def __init__(self, bridge: AudioStreamBridge, direction: str, logger: BoundLogger):
        super().__init__()
        self._bridge = bridge
        self._direction = direction
        self._logger = logger
        self._loop = None
        # Logging counters
        self._frame_request_count = 0
        self._frame_receive_count = 0
        self._silence_count = 0
        self._audio_count = 0
        self._last_log_time = 0
        self._slow_frame_count = 0
        self._error_count = 0

        fmt = pj.MediaFormatAudio()
        fmt.type = pj.PJMEDIA_TYPE_AUDIO
        fmt.clockRate = 8000
        fmt.channelCount = 1
        fmt.bitsPerSample = 16
        fmt.frameTimeUsec = 20000

        port_name = f"voicelive_{direction}"
        self.createPort(port_name, fmt)

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def onFrameRequested(self, frame: pj.MediaFrame) -> None:
        """Called by pjsua when it needs audio to send to caller (from Voice Live).
        
        Uses synchronous dequeue_sip_audio_sync() which reads directly from a
        thread-safe queue, eliminating asyncio cross-thread overhead that was
        causing audio dropouts.
        """
        import time
        self._frame_request_count += 1
        start_time = time.time()
        
        if self._direction == "from_voicelive":
            try:
                # Direct synchronous call - NO asyncio overhead!
                # The bridge uses a thread-safe queue.Queue for outbound audio
                pcm_data = self._bridge.dequeue_sip_audio_sync()
                
                elapsed_ms = (time.time() - start_time) * 1000
                
                expected_bytes = 320
                if len(pcm_data) != expected_bytes:
                    if len(pcm_data) < expected_bytes:
                        pcm_data = pcm_data + b'\x00' * (expected_bytes - len(pcm_data))
                    else:
                        pcm_data = pcm_data[:expected_bytes]

                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.buf = pj.ByteVector(pcm_data)
                frame.size = len(pcm_data)
                
                # Track if this is silence or real audio
                is_silence = pcm_data == b'\x00' * 320
                if is_silence:
                    self._silence_count += 1
                else:
                    self._audio_count += 1
                
                # Track slow frames (>5ms is concerning for 20ms frame interval)
                if elapsed_ms > 5.0:
                    self._slow_frame_count += 1
                
                # Log every second
                now = time.time()
                if now - self._last_log_time >= 1.0:
                    self._logger.info(
                        "media.frame_request_stats",
                        direction=self._direction,
                        total_requests=self._frame_request_count,
                        audio_frames=self._audio_count,
                        silence_frames=self._silence_count,
                        slow_frames=self._slow_frame_count,
                        errors=self._error_count,
                        last_elapsed_ms=round(elapsed_ms, 2),
                    )
                    self._last_log_time = now
                
            except Exception as e:
                self._error_count += 1
                self._logger.warning(
                    "media.frame_request_error",
                    error=str(e),
                    request_num=self._frame_request_count,
                    error_count=self._error_count,
                )
                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.buf = pj.ByteVector(b'\x00' * 320)
                frame.size = 320

    def onFrameReceived(self, frame: pj.MediaFrame) -> None:
        """Called by pjsua when it receives audio from caller (to Voice Live)."""
        import time
        self._frame_receive_count += 1
        
        if self._direction == "to_voicelive" and self._loop:
            if frame.type == pj.PJMEDIA_FRAME_TYPE_AUDIO and frame.buf:
                try:
                    frame_bytes = bytes(frame.buf)
                    
                    # Log every second
                    now = time.time()
                    if now - self._last_log_time >= 1.0:
                        self._logger.info(
                            "media.frame_receive_stats",
                            direction=self._direction,
                            total_received=self._frame_receive_count,
                            frame_bytes=len(frame_bytes),
                        )
                        self._last_log_time = now
                    
                    asyncio.run_coroutine_threadsafe(
                        self._bridge.enqueue_sip_audio(frame_bytes),
                        self._loop
                    )
                except Exception as e:
                    self._error_count += 1
                    self._logger.warning(
                        "media.enqueue_failed",
                        error=str(e),
                        receive_num=self._frame_receive_count,
                        error_count=self._error_count,
                    )


class GatewayCall(pj.Call):
    """Handles SIP call lifecycle and connects media bridge."""

    def __init__(self, account, call_id: int, logger: BoundLogger, settings: Settings,
                 loop: asyncio.AbstractEventLoop):
        super().__init__(account, call_id)
        self._account = account
        self._logger = logger
        self._settings = settings
        self._loop = loop
        self._to_voicelive_port = None
        self._from_voicelive_port = None
        self._event_task = None
        self._bridge: Optional[AudioStreamBridge] = None
        self._voicelive_client: Optional[VoiceLiveClient] = None

    def onCallState(self, prm: pj.OnCallStateParam) -> None:
        ci = self.getInfo()
        self._logger.info(
            "sip.call_state",
            remote_uri=ci.remoteUri,
            state=ci.stateText,
            code=ci.lastStatusCode,
        )
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self._cleanup_media_ports()
            asyncio.run_coroutine_threadsafe(self._cleanup_async_resources(), self._loop)
            self._account.remove_call(self)

    def onCallMediaState(self, prm: pj.OnCallMediaStateParam) -> None:
        ci = self.getInfo()
        for mi in ci.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                media = self.getMedia(mi.index)
                aud_media = pj.AudioMedia.typecastFromMedia(media)

                self._bridge = AudioStreamBridge(self._settings)
                self._voicelive_client = VoiceLiveClient(self._settings)

                self._to_voicelive_port = CustomAudioMediaPort(self._bridge, "to_voicelive", self._logger)
                self._from_voicelive_port = CustomAudioMediaPort(self._bridge, "from_voicelive", self._logger)

                self._to_voicelive_port.set_event_loop(self._loop)
                self._from_voicelive_port.set_event_loop(self._loop)

                # Connect the call audio to our custom ports
                # aud_media -> to_voicelive_port: caller's audio goes to Voice Live
                # from_voicelive_port -> aud_media: Voice Live audio goes to caller
                try:
                    aud_media.startTransmit(self._to_voicelive_port)
                    self._from_voicelive_port.startTransmit(aud_media)
                    self._logger.info("sip.media_connected", 
                                      to_voicelive_port_id=self._to_voicelive_port.getPortId(),
                                      from_voicelive_port_id=self._from_voicelive_port.getPortId())
                except Exception as e:
                    self._logger.error("sip.media_connect_failed", error=str(e))
                    return

                asyncio.run_coroutine_threadsafe(
                    self._initialize_voicelive(),
                    self._loop
                )

                self._logger.info("sip.media_active", message="Initializing Voice Live bridge")
                break
        else:
            self._logger.info("sip.media_inactive")

    async def _initialize_voicelive(self) -> None:
        """Initialize Voice Live connection and start event processing."""
        try:
            await self._voicelive_client.connect()
            await self._bridge.attach_voicelive_client(self._voicelive_client)
            self._event_task = asyncio.create_task(self._process_voicelive_events())
            await self._voicelive_client.request_response(additional_instructions = "Greet the user and wait for their input.")
            self._logger.info("sip.voice_live_bridge_established")
        except Exception as e:
            self._logger.error("sip.initialization_failed", error=str(e))
            await self._cleanup_async_resources()

    async def _process_voicelive_events(self) -> None:
        """Process Voice Live SDK events directly - simplified pattern from Azure sample."""
        import time
        try:
            connection = self._voicelive_client.connection
            pending_function_call = None  # Track active function call
            waiting_for_response_done = False  # Track if we need to wait for RESPONSE_DONE
            
            # Logging counters
            event_count = 0
            audio_delta_count = 0
            total_audio_bytes = 0
            response_count = 0
            last_audio_time = None
            last_log_time = time.time()
            audio_gap_warnings = 0
            
            self._logger.info("voicelive.event_loop_started")

            async for event in connection:
                event_count += 1
                event_time = time.time()
                # Handle function calling
                if isinstance(event, ServerEventConversationItemCreated):
                    if isinstance(event.item, ResponseFunctionCallItem):
                        self._logger.info(
                            "sip.function_call_detected",
                            function=event.item.name,
                            call_id=event.item.call_id
                        )
                        # Store function call info - we'll execute when args arrive
                        pending_function_call = await self._voicelive_client.tool_handler.handle_function_call(
                            event, connection
                        )
                        continue

                # Handle function call arguments completion
                if event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
                    if pending_function_call and event.call_id == pending_function_call["call_id"]:
                        self._logger.info("sip.function_arguments_done", call_id=event.call_id)
                        # Store arguments and wait for RESPONSE_DONE
                        pending_function_call["arguments"] = event.arguments
                        waiting_for_response_done = True
                        continue

                # Execute function after RESPONSE_DONE
                if event.type == ServerEventType.RESPONSE_DONE and waiting_for_response_done:
                    if pending_function_call:
                        self._logger.info("sip.executing_function", function=pending_function_call["function_name"])
                        await self._voicelive_client.tool_handler.execute_function_call(
                            function_name=pending_function_call["function_name"],
                            call_id=pending_function_call["call_id"],
                            arguments=pending_function_call.get("arguments", "{}"),
                            previous_item_id=pending_function_call["previous_item_id"],
                            connection=connection
                        )
                        pending_function_call = None
                        waiting_for_response_done = False
                        continue

                # Handle audio events
                if event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
                    audio_delta_count += 1
                    audio_bytes = event.delta
                    
                    # Detect gaps between audio chunks
                    if last_audio_time is not None:
                        gap_ms = (event_time - last_audio_time) * 1000
                        if gap_ms > 100:  # More than 100ms gap is concerning
                            audio_gap_warnings += 1
                            self._logger.warning(
                                "voicelive.audio_gap_detected",
                                gap_ms=round(gap_ms, 2),
                                delta_num=audio_delta_count,
                                total_gaps=audio_gap_warnings,
                            )
                    last_audio_time = event_time
                    
                    if audio_bytes:
                        try:
                            # Check if audio_bytes is base64 string or already bytes
                            if isinstance(audio_bytes, str):
                                pcm_data = base64.b64decode(audio_bytes)
                            else:
                                pcm_data = audio_bytes
                            
                            total_audio_bytes += len(pcm_data)
                            
                            # Log audio stats every second
                            now = time.time()
                            if now - last_log_time >= 1.0:
                                self._logger.info(
                                    "voicelive.audio_stream_stats",
                                    total_events=event_count,
                                    audio_deltas=audio_delta_count,
                                    total_audio_bytes=total_audio_bytes,
                                    responses=response_count,
                                    gap_warnings=audio_gap_warnings,
                                    last_chunk_bytes=len(pcm_data),
                                )
                                last_log_time = now
                            
                            await self._bridge.emit_audio_to_sip(pcm_data)
                        except Exception as e:
                            self._logger.warning(
                                "sip.audio_decode_failed",
                                error=str(e),
                                delta_num=audio_delta_count,
                            )
                    else:
                        self._logger.debug(
                            "voicelive.empty_audio_delta",
                            delta_num=audio_delta_count,
                        )

                elif event.type == ServerEventType.RESPONSE_CREATED:
                    response_count += 1
                    self._logger.info(
                        "voicelive.response_started",
                        response_num=response_count,
                        total_events=event_count,
                    )

                elif event.type == ServerEventType.RESPONSE_DONE:
                    if not waiting_for_response_done:
                        self._logger.info(
                            "voicelive.response_complete",
                            response_num=response_count,
                            audio_deltas_so_far=audio_delta_count,
                            total_audio_bytes=total_audio_bytes,
                        )

                elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                    self._logger.info("sip.user_speech_started")
                    self._bridge.clear_outbound_queue()

                elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                    self._logger.info("sip.user_speech_stopped")

                elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                    if hasattr(event, "transcript"):
                        self._logger.info("sip.transcription_completed", transcript=event.transcript)

                elif event.type == ServerEventType.ERROR:
                    self._logger.error("sip.voicelive_error", error=event.error.message if hasattr(event, "error") else str(event))

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._logger.error("sip.event_processing_error", error=str(e), traceback=traceback.format_exc())

    def _cleanup_media_ports(self) -> None:
        """Clean up pjlib media ports."""
        if self._to_voicelive_port:
            self._to_voicelive_port = None
        if self._from_voicelive_port:
            self._from_voicelive_port = None

    async def _cleanup_async_resources(self) -> None:
        """Clean up async resources."""
        if self._event_task:
            self._event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._event_task
            self._event_task = None

        if self._voicelive_client:
            await self._voicelive_client.close()
            self._voicelive_client = None

        if self._bridge:
            await self._bridge.close()
            self._bridge = None

        self._logger.info("sip.call_cleanup_complete")


class GatewayAccount(pj.Account):
    """Account that supports unlimited concurrent calls."""

    def __init__(self, logger: BoundLogger, settings: Settings, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._logger = logger
        self._settings = settings
        self._loop = loop
        self.active_calls: dict[int, GatewayCall] = {}

    def onIncomingCall(self, prm: pj.OnIncomingCallParam) -> None:
        self._logger.info("sip.incoming_call", active_calls=len(self.active_calls))
        call = GatewayCall(self, prm.callId, self._logger, self._settings, self._loop)

        self.active_calls[prm.callId] = call

        ringing_param = pj.CallOpParam()
        ringing_param.statusCode = 180
        call.answer(ringing_param)

        accept_param = pj.CallOpParam()
        accept_param.statusCode = 200
        call.answer(accept_param)

    def remove_call(self, call: GatewayCall) -> None:
        """Remove a call from active calls tracking."""
        call_info = call.getInfo()
        if call_info.id in self.active_calls:
            del self.active_calls[call_info.id]
            self._logger.info("sip.call_removed", call_id=call_info.id, remaining_calls=len(self.active_calls))


class SipAgent:
    """Coordinates pjsua signaling with RTP/Voice Live bridging."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger: BoundLogger = structlog.get_logger(__name__)
        self._ep: Optional[pj.Endpoint] = None
        self._transport: Optional[pj.TransportConfig] = None
        self._account: Optional[GatewayAccount] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return

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
        self._logger.info("sip.agent_stopped")

    def _run_pjsua_thread(self, loop: asyncio.AbstractEventLoop) -> None:
        """Runs pjsua2 event loop in a dedicated thread."""
        try:
            self._ep = pj.Endpoint()
            self._ep.libCreate()

            ep_cfg = pj.EpConfig()
            ep_cfg.logConfig.level = 4  # Increase log level for debugging
            ep_cfg.logConfig.consoleLevel = 4
            
            # Configure RTP/media port range for NAT traversal
            ep_cfg.medConfig.rxDropPct = 0
            ep_cfg.medConfig.txDropPct = 0
            ep_cfg.medConfig.noVad = True
            
            # Configure STUN for NAT traversal (only if ICE is enabled)
            if self._settings.sip.enable_ice:
                stun_srv = f"{self._settings.sip.stun_server}:{self._settings.sip.stun_port}"
                ep_cfg.uaConfig.stunServer.append(stun_srv)
                self._logger.info("sip.stun_configuration", stun_server=stun_srv)
            
            self._ep.libInit(ep_cfg)
            
            # Enable null audio device for headless/Docker environments
            # This MUST be called immediately after libInit() before any media operations
            # The null device provides a software clock for the conference bridge
            self._ep.audDevManager().setNullDev()
            self._logger.info("sip.null_audio_device_enabled")
            
            transport_cfg = pj.TransportConfig()
            transport_cfg.port = self._settings.sip.port
            # Bind to all interfaces for Docker networking
            transport_cfg.boundAddress = self._settings.sip.local_address
            
            # Configure public address if behind NAT (for Docker host networking)
            if self._settings.sip.via_address and self._settings.sip.via_address != "0.0.0.0":
                transport_cfg.publicAddress = self._settings.sip.via_address
            
            # Create both UDP and TCP transports for better Docker compatibility
            # TCP is preferred for Docker as it maintains connection state for proper response routing
            self._ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, transport_cfg)
            self._ep.transportCreate(pj.PJSIP_TRANSPORT_TCP, transport_cfg)
            self._ep.libStart()
            self._logger.info("sip.transport_created", port=self._settings.sip.port,
                            public_address=transport_cfg.publicAddress if transport_cfg.publicAddress else "none")

            self._account = GatewayAccount(self._logger, self._settings, loop)

            if self._settings.sip.register_with_server and self._settings.sip.server:
                acc_cfg = pj.AccountConfig()
                acc_cfg.idUri = f"sip:{self._settings.sip.user}@{self._settings.sip.server}"
                acc_cfg.regConfig.registrarUri = f"sip:{self._settings.sip.server}"
                
                # Configure media transport for NAT traversal
                acc_cfg.mediaConfig.transportConfig.port = self._settings.sip.media_port
                acc_cfg.mediaConfig.transportConfig.portRange = self._settings.sip.port_count
                
                # Set public/bound address for media if specified (important for Docker/NAT)
                if self._settings.sip.media_address and self._settings.sip.media_address != "0.0.0.0":
                    acc_cfg.mediaConfig.transportConfig.publicAddress = self._settings.sip.media_address
                elif self._settings.sip.via_address and self._settings.sip.via_address != "0.0.0.0":
                    acc_cfg.mediaConfig.transportConfig.publicAddress = self._settings.sip.via_address
                
                # Enable ICE for NAT traversal if configured
                if self._settings.sip.enable_ice:
                    acc_cfg.natConfig.iceEnabled = True
                    acc_cfg.natConfig.turnEnabled = False
                    acc_cfg.natConfig.sipStunUse = pj.PJSUA_STUN_USE_DEFAULT
                    acc_cfg.natConfig.mediaStunUse = pj.PJSUA_STUN_USE_DEFAULT
                    self._logger.info("sip.ice_and_stun_enabled")
                
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
                
                # Configure media transport for NAT traversal (even without registration)
                acc_cfg.mediaConfig.transportConfig.port = self._settings.sip.media_port
                acc_cfg.mediaConfig.transportConfig.portRange = self._settings.sip.port_count
                
                # Set public/bound address for media if specified (important for Docker/NAT)
                if self._settings.sip.media_address and self._settings.sip.media_address != "0.0.0.0":
                    acc_cfg.mediaConfig.transportConfig.publicAddress = self._settings.sip.media_address
                    self._logger.info("sip.media_public_address_set", address=self._settings.sip.media_address)
                elif self._settings.sip.via_address and self._settings.sip.via_address != "0.0.0.0":
                    acc_cfg.mediaConfig.transportConfig.publicAddress = self._settings.sip.via_address
                    self._logger.info("sip.media_public_address_set", address=self._settings.sip.via_address)
                
                # Enable ICE for NAT traversal if configured
                if self._settings.sip.enable_ice:
                    acc_cfg.natConfig.iceEnabled = True
                    acc_cfg.natConfig.turnEnabled = False
                    # Enable STUN for public address discovery
                    acc_cfg.natConfig.sipStunUse = pj.PJSUA_STUN_USE_DEFAULT
                    acc_cfg.natConfig.mediaStunUse = pj.PJSUA_STUN_USE_DEFAULT
                    self._logger.info("sip.ice_and_stun_enabled")
                
                self._account.create(acc_cfg)
            
            self._logger.info(
                "sip.media_config",
                media_port=self._settings.sip.media_port,
                port_range=self._settings.sip.port_count
            )

            while self._running:
                self._ep.libHandleEvents(100)

        except Exception as exc:
            import traceback
            self._logger.error("sip.thread_error", error=str(exc), type=type(exc).__name__, traceback=traceback.format_exc())
