"""Efficient per-call WAV recorder with mono mixdown at 8 kHz PCM16.

Design notes
─────────────
• Two separate ``bytearray`` buffers collect caller and AI audio in lock-free
  fashion (each buffer has a single writer thread, no contention).
• ``finalize()`` mixes, clips, and writes a standard WAV via the stdlib
  ``wave`` module — zero external dependencies.
• A 30-minute default cap bounds peak memory to ≈ 57 MB per call
  (two 28.8 MB buffers).  The cap is configurable.
• Disk-space is checked before writing; the call is never interrupted —
  only the recording is skipped with a warning log.
"""

from __future__ import annotations

import os
import re
import shutil
import wave
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import structlog
from structlog.stdlib import BoundLogger

from voicelive_sip_gateway.config.settings import RecordingSettings

# 8 kHz mono PCM16: 16 000 bytes per second (8000 samples × 2 bytes)
_SAMPLE_RATE = 8000
_BYTES_PER_SAMPLE = 2
_BYTES_PER_SEC = _SAMPLE_RATE * _BYTES_PER_SAMPLE

# Regex that keeps '+', digits, and hyphens in caller IDs
_SAFE_CALLER_RE = re.compile(r"[^\w+\-]")


class CallRecorder:
    """Accumulates caller + AI PCM16-8 kHz frames and writes a mixed WAV on ``finalize()``."""

    __slots__ = (
        "_caller_buf",
        "_ai_buf",
        "_max_bytes",
        "_closed",
        "_cap_logged",
        "_recording_dir",
        "_min_disk_mb",
        "_caller_id",
        "_start_ts",
        "_logger",
    )

    def __init__(self, caller_number: str, settings: RecordingSettings) -> None:
        self._logger: BoundLogger = structlog.get_logger(__name__).bind(caller=caller_number)

        self._caller_id = _SAFE_CALLER_RE.sub("", caller_number) or "unknown"
        self._start_ts = datetime.now(tz=timezone.utc)

        self._recording_dir = settings.directory
        os.makedirs(self._recording_dir, exist_ok=True)

        self._max_bytes = settings.max_duration_sec * _BYTES_PER_SEC
        self._min_disk_mb = settings.min_disk_mb

        self._caller_buf = bytearray()
        self._ai_buf = bytearray()
        self._closed = False
        self._cap_logged = False

        self._logger.info(
            "recorder.created",
            max_duration_sec=settings.max_duration_sec,
            recording_dir=self._recording_dir,
        )

    # ── frame writers (hot path — no locks needed) ──────────────────────

    def append_caller_frame(self, frame: bytes) -> None:
        """Append a PCM16-8 kHz frame from the caller. Called from the asyncio thread."""
        if self._closed:
            return
        if len(self._caller_buf) >= self._max_bytes:
            if not self._cap_logged:
                self._logger.warning("recorder.caller_cap_reached", bytes=len(self._caller_buf))
                self._cap_logged = True
            return
        self._caller_buf.extend(frame)

    def append_ai_frame(self, frame: bytes) -> None:
        """Append a PCM16-8 kHz frame of AI audio. Called from the pjsua thread."""
        if self._closed:
            return
        if len(self._ai_buf) >= self._max_bytes:
            if not self._cap_logged:
                self._logger.warning("recorder.ai_cap_reached", bytes=len(self._ai_buf))
                self._cap_logged = True
            return
        self._ai_buf.extend(frame)

    # ── finalize (runs in executor after call ends) ─────────────────────

    def finalize(self) -> Optional[str]:
        """Mix both buffers and write a WAV file.  Returns the path or *None* on skip.

        This is a **blocking** call (disk I/O) and should be dispatched via
        ``loop.run_in_executor``.
        """
        self._closed = True

        caller_len = len(self._caller_buf)
        ai_len = len(self._ai_buf)

        if caller_len == 0 and ai_len == 0:
            self._logger.info("recorder.skip_empty")
            return None

        # ── disk-space guard ────────────────────────────────────────────
        try:
            usage = shutil.disk_usage(self._recording_dir)
            free_mb = usage.free / (1024 * 1024)
            if free_mb < self._min_disk_mb:
                self._logger.error(
                    "recorder.low_disk_space",
                    free_mb=round(free_mb, 1),
                    threshold_mb=self._min_disk_mb,
                )
                self._release_buffers()
                return None
        except OSError as exc:
            self._logger.warning("recorder.disk_check_failed", error=str(exc))

        # ── build numpy arrays & zero-pad the shorter one ───────────────
        caller = np.frombuffer(self._caller_buf, dtype=np.int16)
        ai = np.frombuffer(self._ai_buf, dtype=np.int16)

        max_len = max(len(caller), len(ai))
        if len(caller) < max_len:
            caller = np.pad(caller, (0, max_len - len(caller)))
        if len(ai) < max_len:
            ai = np.pad(ai, (0, max_len - len(ai)))

        # Summation with clipping to prevent int16 overflow
        mixed = np.clip(
            caller.astype(np.int32) + ai.astype(np.int32),
            -32768,
            32767,
        ).astype(np.int16)

        # ── write WAV ──────────────────────────────────────────────────
        timestamp_str = self._start_ts.strftime("%Y%m%d_%H%M%S")
        filename = f"call_{self._caller_id}_{timestamp_str}.wav"
        filepath = os.path.join(self._recording_dir, filename)

        with wave.open(filepath, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(_BYTES_PER_SAMPLE)
            wf.setframerate(_SAMPLE_RATE)
            wf.writeframes(mixed.tobytes())

        file_size = os.path.getsize(filepath)
        duration_sec = round(max_len / _SAMPLE_RATE, 1)

        self._logger.info(
            "recorder.saved",
            path=filepath,
            size_bytes=file_size,
            duration_sec=duration_sec,
            caller_samples=len(np.frombuffer(self._caller_buf, dtype=np.int16)) if caller_len else 0,
            ai_samples=len(np.frombuffer(self._ai_buf, dtype=np.int16)) if ai_len else 0,
        )

        self._release_buffers()
        return filepath

    # ── helpers ─────────────────────────────────────────────────────────

    def _release_buffers(self) -> None:
        """Eagerly free memory held by the raw audio buffers."""
        self._caller_buf = bytearray()
        self._ai_buf = bytearray()
