"""Azure Voice Live async client wrapper - simplified with direct event handling."""
from __future__ import annotations

import time
from typing import Optional

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AzureStandardVoice,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerVad,
    AudioInputTranscriptionOptions,
    ToolChoiceLiteral,
    ServerEventConversationItemCreated,
    ResponseFunctionCallItem,
)
from azure.core.credentials import AzureKeyCredential
from azure.identity.aio import DefaultAzureCredential
from structlog.stdlib import BoundLogger
import structlog

from voicelive_sip_gateway.config.settings import Settings
from voicelive_sip_gateway.voicelive.tools import ToolHandler


class VoiceLiveClient:
    """Simplified Azure Voice Live client with direct SDK event handling."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connection_cm = None
        self._connection = None
        self._aad_credential = None
        self._logger: BoundLogger = structlog.get_logger(__name__)
        self._tool_handler = ToolHandler()
        # Logging counters
        self._audio_chunks_sent = 0
        self._total_bytes_sent = 0
        self._last_send_log_time = time.time()
        self._send_errors = 0
        self._connect_time = None

    async def __aenter__(self) -> "VoiceLiveClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.close()

    async def connect(self) -> None:
        """Connect to Azure Voice Live and configure session."""
        if self._connection:
            return

        connect_start = time.time()
        self._logger.info("voicelive.connecting", endpoint=self._settings.azure.endpoint)

        if self._settings.azure.api_key:
            credential = AzureKeyCredential(self._settings.azure.api_key)
        else:
            self._aad_credential = DefaultAzureCredential()
            credential = await self._aad_credential.__aenter__()

        self._connection_cm = connect(
            endpoint=self._settings.azure.endpoint,
            credential=credential,
            model=self._settings.azure.model
        )
        self._connection = await self._connection_cm.__aenter__()
        
        ws_connect_time = time.time() - connect_start
        self._logger.info("voicelive.websocket_connected", connect_ms=round(ws_connect_time * 1000, 2))

        # Configure session with function tools
        session_start = time.time()
        session = RequestSession(
            model="gpt-4o",
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions=self._settings.azure.instructions,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            input_audio_transcription=AudioInputTranscriptionOptions(model="azure-speech"),
            turn_detection=ServerVad(threshold=0.5, prefix_padding_ms=200, silence_duration_ms=400),
            voice=AzureStandardVoice(name=self._settings.azure.voice),
            tools=self._tool_handler.get_tools(),
            tool_choice=ToolChoiceLiteral.AUTO,
        )
        #log all the settings used by the request session
        self._logger.debug("voicelive.session_config", instructions=self._settings.azure.instructions)
        await self._connection.session.update(session=session)
        
        session_config_time = time.time() - session_start
        total_connect_time = time.time() - connect_start
        self._connect_time = time.time()
        
        self._logger.info(
            "voicelive.connected",
            endpoint=self._settings.azure.endpoint,
            model=self._settings.azure.model,
            session_config_ms=round(session_config_time * 1000, 2),
            total_connect_ms=round(total_connect_time * 1000, 2),
        )

    async def close(self) -> None:
        """Close the Voice Live connection."""
        if self._connection_cm and self._connection:
            uptime = time.time() - self._connect_time if self._connect_time else 0
            self._logger.info(
                "voicelive.disconnecting",
                total_chunks_sent=self._audio_chunks_sent,
                total_bytes_sent=self._total_bytes_sent,
                total_errors=self._send_errors,
                uptime_seconds=round(uptime, 1),
            )
            await self._connection_cm.__aexit__(None, None, None)
            self._connection = None
            self._connection_cm = None
            self._logger.info("voicelive.disconnected")

        if self._aad_credential is not None:
            await self._aad_credential.__aexit__(None, None, None)
            self._aad_credential = None

    async def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        """Send audio data to Voice Live."""
        if not self._connection:
            self._send_errors += 1
            self._logger.error(
                "voicelive.send_failed",
                reason="connection not established",
                error_count=self._send_errors,
            )
            raise RuntimeError("VoiceLive connection not established")
        
        self._audio_chunks_sent += 1
        self._total_bytes_sent += len(pcm_bytes)
        send_start = time.time()
        
        try:
            await self._connection.input_audio_buffer.append(audio=pcm_bytes)
            send_elapsed = time.time() - send_start
            
            # Log every second
            now = time.time()
            if now - self._last_send_log_time >= 1.0:
                uptime = now - self._connect_time if self._connect_time else 0
                self._logger.info(
                    "voicelive.audio_send_stats",
                    chunks_sent=self._audio_chunks_sent,
                    total_bytes_sent=self._total_bytes_sent,
                    last_chunk_bytes=len(pcm_bytes),
                    last_send_ms=round(send_elapsed * 1000, 2),
                    errors=self._send_errors,
                    uptime_seconds=round(uptime, 1),
                )
                self._last_send_log_time = now
                
        except Exception as e:
            self._send_errors += 1
            self._logger.error(
                "voicelive.send_audio_error",
                error=str(e),
                chunk_num=self._audio_chunks_sent,
                error_count=self._send_errors,
            )
            raise

    async def request_response(self, additional_instructions) -> None:
        """Request a response from the AI."""
        if not self._connection:
            raise RuntimeError("VoiceLive connection not established")
        await self._connection.response.create(additional_instructions = additional_instructions)

    @property
    def tool_handler(self) -> ToolHandler:
        """Get the tool handler for function calling."""
        return self._tool_handler

    @property
    def connection(self):
        """Get the underlying SDK connection."""
        return self._connection
