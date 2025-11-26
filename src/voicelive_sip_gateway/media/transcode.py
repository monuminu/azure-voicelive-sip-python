"""Audio transcoding helpers (PCM16 <-> μ-law) plus resampling utilities."""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
from scipy.signal import resample_poly

_MAX_16BIT = 32767
_MU = 255


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

    if source_rate == target_rate:
        return pcm_bytes

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    factor = math.gcd(target_rate, source_rate)
    up = target_rate // factor
    down = source_rate // factor
    resampled = resample_poly(samples, up=up, down=down)
    clipped = np.clip(resampled, -_MAX_16BIT, _MAX_16BIT).astype(np.int16)
    return clipped.tobytes()
