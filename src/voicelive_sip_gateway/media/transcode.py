"""Audio transcoding helpers (PCM16 <-> μ-law) plus resampling utilities."""
from __future__ import annotations

import math
import time
from typing import Iterable

import numpy as np
from scipy.signal import resample_poly
import structlog

_MAX_16BIT = 32767
_MU = 255
_logger = structlog.get_logger(__name__)

# Logging counters for resampling stats
_resample_call_count = 0
_last_resample_log_time = time.time()


def _linear_to_mulaw_sample(sample: int) -> int:
    sign = 0
    if sample < 0:
        sign = 0x80
        sample = -sample
    sample = min(sample, _MAX_16BIT)
    magnitude = math.log1p(_MU * sample / _MAX_16BIT) / math.log1p(_MU)
    encoded = int(magnitude * 127) & 0x7F
    return (~(sign | encoded)) & 0xFF


def _mulaw_to_linear_sample(code: int) -> int:
    code = ~code & 0xFF
    sign = code & 0x80
    exponent = (code >> 4) & 0x07
    mantissa = code & 0x0F
    magnitude = ((mantissa << 4) + 8) << exponent
    sample = magnitude - 132
    return -sample if sign else sample


def pcm16_to_mulaw(pcm_bytes: bytes | Iterable[int]) -> bytes:
    """Convert signed 16-bit PCM samples to μ-law encoded bytes."""

    if isinstance(pcm_bytes, bytes):
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    else:
        samples = np.fromiter(pcm_bytes, dtype=np.int16)
    encoded = bytearray(_linear_to_mulaw_sample(int(sample)) for sample in samples)
    return bytes(encoded)


def mulaw_to_pcm16(mulaw_bytes: bytes | Iterable[int]) -> bytes:
    """Convert μ-law bytes back to signed 16-bit PCM samples."""

    samples = bytearray()
    for code in mulaw_bytes:
        sample = _mulaw_to_linear_sample(int(code))
        samples.extend(int(sample).to_bytes(2, byteorder="little", signed=True))
    return bytes(samples)


def resample_pcm16(pcm_bytes: bytes, source_rate: int, target_rate: int) -> bytes:
    """Resample PCM16 bytes from source_rate to target_rate using polyphase filtering."""
    global _resample_call_count, _last_resample_log_time
    _resample_call_count += 1
    
    if source_rate == target_rate:
        return pcm_bytes

    start_time = time.time()
    input_len = len(pcm_bytes)
    
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    factor = math.gcd(target_rate, source_rate)
    up = target_rate // factor
    down = source_rate // factor
    
    # Track min/max for clipping detection
    input_max = np.max(np.abs(samples)) if len(samples) > 0 else 0
    
    resampled = resample_poly(samples, up=up, down=down)
    
    output_max = np.max(np.abs(resampled)) if len(resampled) > 0 else 0
    clipped_count = np.sum(np.abs(resampled) > _MAX_16BIT) if len(resampled) > 0 else 0
    
    clipped = np.clip(resampled, -_MAX_16BIT, _MAX_16BIT).astype(np.int16)
    result = clipped.tobytes()
    
    elapsed_ms = (time.time() - start_time) * 1000
    
    # Log every second or if there are issues
    now = time.time()
    should_log = (now - _last_resample_log_time >= 1.0) or clipped_count > 0 or elapsed_ms > 5.0
    
    if should_log:
        _logger.info(
            "transcode.resample",
            call_num=_resample_call_count,
            direction=f"{source_rate}->{target_rate}",
            up=up,
            down=down,
            input_bytes=input_len,
            input_samples=len(samples),
            output_samples=len(clipped),
            output_bytes=len(result),
            input_max=round(float(input_max), 1),
            output_max=round(float(output_max), 1),
            clipped_samples=int(clipped_count),
            elapsed_ms=round(elapsed_ms, 2),
        )
        _last_resample_log_time = now
    
    return result
