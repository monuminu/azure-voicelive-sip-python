"""Minimal SDP helpers for μ-law (PCMU/8000) audio sessions."""
from __future__ import annotations

from dataclasses import dataclass


SDP_TEMPLATE = """v=0\no=- 0 0 IN IP4 {address}\ns=Voice Live SIP Gateway\nc=IN IP4 {address}\nt=0 0\nm=audio {port} RTP/AVP {payload_type}\na=rtpmap:{payload_type} PCMU/8000\na=ptime:{ptime}\n"""


@dataclass(slots=True)
class SessionDescription:
    """Represents the subset of SDP values we care about."""

    address: str
    port: int
    payload_type: int = 0
    codec: str = "PCMU/8000"


class SdpError(ValueError):
    """Raised when SDP parsing or generation fails."""


def build_sdp(address: str, port: int, payload_type: int = 0, ptime: int = 20) -> str:
    """Produce a simple SDP answer for μ-law RTP/AVP."""

    if not address:
        raise SdpError("address is required for SDP generation")
    if port <= 0:
        raise SdpError("port must be > 0")
    return SDP_TEMPLATE.format(address=address, port=port, payload_type=payload_type, ptime=ptime)


def parse_sdp(body: str) -> SessionDescription:
    """Parse a remote SDP offer/answer and extract RTP endpoint info."""

    if not body:
        raise SdpError("SDP body is empty")

    address: str | None = None
    port: int | None = None
    payload_type = 0
    codec = "PCMU/8000"

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("c="):
            # Example: c=IN IP4 192.168.1.4
            parts = line[2:].strip().split()
            if len(parts) >= 3:
                address = parts[2]
        elif line.startswith("m=audio"):
            parts = line[2:].strip().split()
            if len(parts) < 4:
                raise SdpError("malformed m=audio line")
            try:
                port = int(parts[1])
                payload_type = int(parts[3])
            except ValueError as exc:
                raise SdpError("invalid port or payload in SDP") from exc
        elif line.startswith("a=rtpmap:"):
            try:
                descriptor, encoding = line[9:].split(None, 1)
                pt = int(descriptor)
                if pt == payload_type:
                    codec = encoding
            except ValueError:
                # Ignore malformed rtpmap hints and keep defaults.
                continue

    if address is None or port is None:
        raise SdpError("missing c= or m=audio line")

    return SessionDescription(address=address, port=port, payload_type=payload_type, codec=codec)
