"""Azure Voice Live async client wrapper - simplified with direct event handling."""
from __future__ import annotations

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

    async def __aenter__(self) -> "VoiceLiveClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.close()

    async def connect(self) -> None:
        """Connect to Azure Voice Live and configure session."""
        if self._connection:
            return

        if self._settings.azure.api_key:
            credential = AzureKeyCredential(self._settings.azure.api_key)
        else:
            self._aad_credential = DefaultAzureCredential()
            credential = await self._aad_credential.__aenter__()

        self._connection_cm = connect(
            endpoint=self._settings.azure.endpoint,
            credential=credential,
            model=self._settings.azure.model,
        )
        self._connection = await self._connection_cm.__aenter__()

        # Configure session with function tools
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
        await self._connection.session.update(session=session)
        self._logger.info("voicelive.connected", endpoint=self._settings.azure.endpoint)

    async def close(self) -> None:
        """Close the Voice Live connection."""
        if self._connection_cm and self._connection:
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
            raise RuntimeError("VoiceLive connection not established")
        await self._connection.input_audio_buffer.append(audio=pcm_bytes)

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
