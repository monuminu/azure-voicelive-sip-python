"""Orchestrates audio movement between SIP RTP streams and Azure Voice Live."""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import queue
import time
from typing import Optional

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
        # Use thread-safe queue for outbound (pjsua reads from its own thread)
        self._outbound_queue: queue.Queue[bytes] = queue.Queue()
        self._task: Optional[asyncio.Task[None]] = None
        self._voicelive_client = None
        # Thread pool for CPU-bound resampling to avoid blocking event loop
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="resample")
        # Logging counters
        self._inbound_count = 0
        self._outbound_count = 0
        self._last_inbound_log_time = time.time()
        self._last_outbound_log_time = time.time()
        self._voicelive_audio_chunks_received = 0
        self._sip_frames_sent = 0
        self._partial_frame_buffer = b''  # Buffer for incomplete frames
        self._logger.info("bridge.initialized")

    async def attach_voicelive_client(self, client) -> None:
        self._voicelive_client = client
        if not self._task:
            self._task = asyncio.create_task(self._flush())

    async def enqueue_sip_audio(self, pcm_payload: bytes) -> None:
        """Queue PCM16 8kHz audio from SIP (pjsua provides PCM16, not μ-law)."""
        self._inbound_count += 1
        queue_size = self._inbound_queue.qsize()
        
        # Log every 50 frames (~1 second) or if queue is backing up
        now = time.time()
        if now - self._last_inbound_log_time >= 1.0 or queue_size > 10:
            self._logger.info(
                "bridge.inbound_stats",
                frames_received=self._inbound_count,
                queue_size=queue_size,
                payload_bytes=len(pcm_payload),
            )
            self._last_inbound_log_time = now
        
        if queue_size > 25:
            self._logger.warning(
                "bridge.inbound_queue_high",
                queue_size=queue_size,
                frames_received=self._inbound_count,
            )
        
        await self._inbound_queue.put(pcm_payload)

    def dequeue_sip_audio_sync(self) -> bytes:
        """Get PCM16 audio for SIP - SYNCHRONOUS, safe to call from pjsua thread.
        
        This method uses a thread-safe queue and does NOT require asyncio,
        eliminating cross-thread scheduling delays that cause audio dropouts.
        """
        self._sip_frames_sent += 1
        
        try:
            # Non-blocking get from thread-safe queue - NO asyncio overhead
            frame = self._outbound_queue.get_nowait()
            
            # Log every second with audio stats
            now = time.time()
            if now - self._last_outbound_log_time >= 1.0:
                self._logger.info(
                    "bridge.outbound_stats",
                    frames_sent=self._sip_frames_sent,
                    queue_size=self._outbound_queue.qsize(),
                    voicelive_chunks=self._voicelive_audio_chunks_received,
                    has_audio=True,
                )
                self._last_outbound_log_time = now
            return frame
        except queue.Empty:
            # Log silence delivery every second
            now = time.time()
            if now - self._last_outbound_log_time >= 1.0:
                self._logger.debug(
                    "bridge.outbound_silence",
                    frames_sent=self._sip_frames_sent,
                    queue_size=0,
                    voicelive_chunks=self._voicelive_audio_chunks_received,
                )
                self._last_outbound_log_time = now
            # Return 20ms of silence (160 samples at 8kHz = 320 bytes)
            return b'\x00' * 320

    async def feed_voicelive_audio(self, pcm_chunk: bytes) -> None:
        if not self._voicelive_client:
            self._logger.warning("voicelive.audio_drop", reason="client not attached")
            return
        await self._voicelive_client.send_audio_chunk(pcm_chunk)

    async def emit_audio_to_sip(self, pcm_chunk: bytes) -> None:
        """Resample Voice Live audio down to 8 kHz PCM frames for SIP playback.
        
        Resampling runs in a thread pool to avoid blocking the asyncio event loop.
        Frames are queued to a thread-safe queue that pjsua reads directly.
        """
        self._voicelive_audio_chunks_received += 1
        start_time = time.time()
        
        # Log incoming Voice Live audio
        self._logger.info(
            "bridge.voicelive_audio_received",
            chunk_num=self._voicelive_audio_chunks_received,
            input_bytes=len(pcm_chunk),
            outbound_queue_before=self._outbound_queue.qsize(),
        )
        
        # Run CPU-intensive resampling in thread pool to avoid blocking event loop
        loop = asyncio.get_running_loop()
        pcm_8k = await loop.run_in_executor(
            self._executor,
            resample_pcm16,
            pcm_chunk,
            self.VOICELIVE_SAMPLE_RATE,
            self.SIP_SAMPLE_RATE
        )
        resample_time = time.time() - start_time
        
        # Prepend any leftover from previous chunk
        pcm_8k = self._partial_frame_buffer + pcm_8k
        
        frame_duration_ms = 20
        samples_per_frame = int(self.SIP_SAMPLE_RATE * frame_duration_ms / 1000)
        frame_size_bytes = samples_per_frame * 2  # PCM16 mono uses 2 bytes per sample
        
        # Calculate how many complete frames we have
        full_frame_count = len(pcm_8k) // frame_size_bytes
        
        frames_queued = 0
        for i in range(full_frame_count):
            frame = pcm_8k[i * frame_size_bytes : (i + 1) * frame_size_bytes]
            # Put directly into thread-safe queue - no await needed
            self._outbound_queue.put_nowait(frame)
            frames_queued += 1
        
        # Save any remainder for next chunk
        remainder_start = full_frame_count * frame_size_bytes
        self._partial_frame_buffer = pcm_8k[remainder_start:]
        
        queue_time = time.time() - start_time - resample_time
        
        self._logger.info(
            "bridge.emit_complete",
            chunk_num=self._voicelive_audio_chunks_received,
            input_bytes=len(pcm_chunk),
            output_bytes=len(pcm_8k),
            frames_queued=frames_queued,
            buffered_bytes=len(self._partial_frame_buffer),
            outbound_queue_after=self._outbound_queue.qsize(),
            resample_ms=round(resample_time * 1000, 2),
            queue_ms=round(queue_time * 1000, 2),
        )
                # # Also play to local speaker for debugging
                # try:
                #     self._speaker_stream.write(frame)
                # except Exception as e:
                #     self._logger.debug("bridge.speaker_error", error=str(e))

    def clear_outbound_queue(self) -> None:
        """Clear all pending audio from the outbound queue (for interruption)."""
        cleared_count = 0
        while not self._outbound_queue.empty():
            try:
                self._outbound_queue.get_nowait()
                cleared_count += 1
            except queue.Empty:
                break
        self._logger.info(
            "bridge.outbound_queue_cleared",
            frames_cleared=cleared_count,
            voicelive_chunks_before_clear=self._voicelive_audio_chunks_received,
        )

    def clear_all_queues(self) -> None:
        """Clear all audio queues (for cleanup)."""
        inbound_cleared = 0
        outbound_cleared = 0
        while not self._inbound_queue.empty():
            try:
                self._inbound_queue.get_nowait()
                inbound_cleared += 1
            except asyncio.QueueEmpty:
                break
        while not self._outbound_queue.empty():
            try:
                self._outbound_queue.get_nowait()
                outbound_cleared += 1
            except queue.Empty:
                break
        self._logger.info(
            "bridge.all_queues_cleared",
            inbound_frames_cleared=inbound_cleared,
            outbound_frames_cleared=outbound_cleared,
            total_inbound_received=self._inbound_count,
            total_outbound_sent=self._sip_frames_sent,
            total_voicelive_chunks=self._voicelive_audio_chunks_received,
        )

    async def _flush(self) -> None:
        """Process inbound audio: PCM16 8kHz from SIP → PCM16 24kHz to Voice Live.
        
        Resampling runs in a thread pool to avoid blocking the event loop.
        """
        flush_count = 0
        last_flush_log_time = time.time()
        loop = asyncio.get_running_loop()
        
        while True:
            pcm16_8k = await self._inbound_queue.get()
            flush_count += 1
            start_time = time.time()
            
            # Run CPU-intensive resampling in thread pool
            pcm16_24k = await loop.run_in_executor(
                self._executor,
                resample_pcm16,
                pcm16_8k,
                self.SIP_SAMPLE_RATE,
                self.VOICELIVE_SAMPLE_RATE
            )
            resample_time = time.time() - start_time
            
            if self._voicelive_client:
                send_start = time.time()
                await self._voicelive_client.send_audio_chunk(pcm16_24k)
                send_time = time.time() - send_start
                
                # Log every second
                now = time.time()
                if now - last_flush_log_time >= 1.0:
                    self._logger.info(
                        "bridge.flush_stats",
                        frames_flushed=flush_count,
                        inbound_queue_size=self._inbound_queue.qsize(),
                        input_bytes=len(pcm16_8k),
                        output_bytes=len(pcm16_24k),
                        resample_ms=round(resample_time * 1000, 2),
                        send_ms=round(send_time * 1000, 2),
                    )
                    last_flush_log_time = now
            else:
                self._logger.warning("voicelive.audio_drop", reason="missing client", flush_count=flush_count)

    async def close(self) -> None:
        self._logger.info(
            "bridge.closing",
            total_inbound_received=self._inbound_count,
            total_outbound_sent=self._sip_frames_sent,
            total_voicelive_chunks=self._voicelive_audio_chunks_received,
            inbound_queue_remaining=self._inbound_queue.qsize(),
            outbound_queue_remaining=self._outbound_queue.qsize(),
        )
        
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        # Shutdown thread pool executor
        self._executor.shutdown(wait=False)

        # Clear all queues
        self.clear_all_queues()

        self._logger.info("bridge.closed")
