"""Azure Voice Live async client wrapper."""
from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator, Optional

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AzureStandardVoice,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerVad,
    AudioInputTranscriptionOptions
)
from azure.core.credentials import AzureKeyCredential
from azure.identity.aio import DefaultAzureCredential
from structlog.stdlib import BoundLogger
import structlog

from voicelive_sip_gateway.config.settings import Settings
from voicelive_sip_gateway.voicelive.events import VoiceLiveEvent, map_sdk_event


class VoiceLiveClient:
    """Manages lifecycle of an Azure Voice Live WebSocket connection."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connection_cm = None
        self._connection = None
        self._aad_credential = None
        self._logger: BoundLogger = structlog.get_logger(__name__)
        self._event_task: Optional[asyncio.Task[None]] = None
        self._event_queue: asyncio.Queue[VoiceLiveEvent] = asyncio.Queue()

    async def __aenter__(self) -> "VoiceLiveClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.close()

    async def connect(self) -> None:
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

        session = RequestSession(
            model="gpt-4o",
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions=self._settings.azure.instructions,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            input_audio_transcription = AudioInputTranscriptionOptions(model= "azure-speech"),
            turn_detection=ServerVad(threshold=0.5, prefix_padding_ms=200, silence_duration_ms=400),
            voice=AzureStandardVoice(name=self._settings.azure.voice)
        )
        await self._connection.session.update(session=session)
        self._event_task = asyncio.create_task(self._drain_events())
        self._logger.info("voicelive.connected", endpoint=self._settings.azure.endpoint)

    async def close(self) -> None:
        if self._event_task:
            self._event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._event_task
            self._event_task = None

        if self._connection_cm and self._connection:
            await self._connection_cm.__aexit__(None, None, None)
            self._connection = None
            self._connection_cm = None
            self._logger.info("voicelive.disconnected")

        if self._aad_credential is not None:
            await self._aad_credential.__aexit__(None, None, None)
            self._aad_credential = None

    async def _drain_events(self) -> None:
        assert self._connection is not None
        async for event in self._connection:
            await self._event_queue.put(map_sdk_event(event))

    async def events(self) -> AsyncIterator[VoiceLiveEvent]:
        while True:
            event = await self._event_queue.get()
            yield event

    async def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        if not self._connection:
            raise RuntimeError("VoiceLive connection not established")
        await self._connection.input_audio_buffer.append(audio=pcm_bytes)

    async def request_response(self, interrupt: bool = False) -> None:
        if not self._connection:
            raise RuntimeError("VoiceLive connection not established")
        await self._connection.response.create()
