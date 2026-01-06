"""Orchestrates audio movement between SIP RTP streams and Azure Voice Live."""
from __future__ import annotations

import asyncio
import contextlib
import wave
from typing import Optional

import pyaudio
from structlog.stdlib import BoundLogger
import structlog

from voicelive_sip_gateway.config.settings import Settings
from voicelive_sip_gateway.media.transcode import resample_pcm16


class AudioStreamBridge:
    """Bidirectional audio pump between SIP (μ-law) and Voice Live (PCM16 24kHz)."""

    VOICELIVE_SAMPLE_RATE = 24000
    SIP_SAMPLE_RATE = 8000

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger: BoundLogger = structlog.get_logger(__name__)
        self._inbound_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._outbound_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._task: Optional[asyncio.Task[None]] = None
        self._voicelive_client = None
        
        # Initialize PyAudio for local playback
        self._pyaudio = pyaudio.PyAudio()
        self._speaker_stream = self._pyaudio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.SIP_SAMPLE_RATE,
            output=True,
            frames_per_buffer=160  # 20ms frames at 8kHz
        )
        self._logger.info("bridge.speaker_enabled", sample_rate=self.SIP_SAMPLE_RATE)

    async def attach_voicelive_client(self, client) -> None:
        self._voicelive_client = client
        if not self._task:
            self._task = asyncio.create_task(self._flush())

    async def enqueue_sip_audio(self, pcm_payload: bytes) -> None:
        """Queue PCM16 8kHz audio from SIP (pjsua provides PCM16, not μ-law)."""
        await self._inbound_queue.put(pcm_payload)

    async def dequeue_sip_audio(self, timeout: float = 0.015) -> bytes:
        """Get PCM16 audio for SIP, returns silence if not available within timeout."""
        try:
            return await asyncio.wait_for(self._outbound_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            # Return 20ms of silence (160 samples at 8kHz = 320 bytes)
            return b'\x00' * 320
    
    async def dequeue_sip_audio_nonblocking(self) -> bytes:
        """Get PCM16 audio for SIP immediately, returns silence if queue is empty."""
        try:
            return self._outbound_queue.get_nowait()
        except asyncio.QueueEmpty:
            # Return 20ms of silence (160 samples at 8kHz = 320 bytes)
            return b'\x00' * 320

    async def feed_voicelive_audio(self, pcm_chunk: bytes) -> None:
        if not self._voicelive_client:
            self._logger.warning("voicelive.audio_drop", reason="client not attached")
            return
        await self._voicelive_client.send_audio_chunk(pcm_chunk)

    async def emit_audio_to_sip(self, pcm_chunk: bytes) -> None:
        """Resample Voice Live audio down to 8 kHz PCM frames for SIP playback."""
        pcm_8k = resample_pcm16(pcm_chunk, self.VOICELIVE_SAMPLE_RATE, self.SIP_SAMPLE_RATE)
        frame_duration_ms = 20
        samples_per_frame = int(self.SIP_SAMPLE_RATE * frame_duration_ms / 1000)
        frame_size_bytes = samples_per_frame * 2  # PCM16 mono uses 2 bytes per sample
        total_frames = (len(pcm_8k) + frame_size_bytes - 1) // frame_size_bytes
        self._logger.info(
            "bridge.emit_chunk",
            voicelive_bytes=len(pcm_chunk),
            sip_bytes=len(pcm_8k),
            frames=total_frames,
        )

        for offset in range(0, len(pcm_8k), frame_size_bytes):
            frame = pcm_8k[offset : offset + frame_size_bytes]
            if frame:
                await self._outbound_queue.put(frame)
                # # Also play to local speaker for debugging
                # try:
                #     self._speaker_stream.write(frame)
                # except Exception as e:
                #     self._logger.debug("bridge.speaker_error", error=str(e))

    def clear_outbound_queue(self) -> None:
        """Clear all pending audio from the outbound queue (for interruption)."""
        while not self._outbound_queue.empty():
            try:
                self._outbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._logger.info("bridge.outbound_queue_cleared")

    async def _flush(self) -> None:
        """Process inbound audio: PCM16 8kHz from SIP → PCM16 24kHz to Voice Live."""
        while True:
            pcm16_8k = await self._inbound_queue.get()
            pcm16_24k = resample_pcm16(pcm16_8k, self.SIP_SAMPLE_RATE, self.VOICELIVE_SAMPLE_RATE)
            if self._voicelive_client:
                await self._voicelive_client.send_audio_chunk(pcm16_24k)
            else:
                self._logger.warning("voicelive.audio_drop", reason="missing client")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        
        # Close speaker stream
        if self._speaker_stream:
            self._speaker_stream.stop_stream()
            self._speaker_stream.close()
        if self._pyaudio:
            self._pyaudio.terminate()
        self._logger.info("bridge.speaker_closed")
