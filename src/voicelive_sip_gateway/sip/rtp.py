"""Lightweight RTP sender/receiver utilities for μ-law audio."""
from __future__ import annotations

import asyncio
import random
import struct
from dataclasses import dataclass
from typing import Optional
import contextlib

from structlog.stdlib import BoundLogger
import structlog

from voicelive_sip_gateway.media.stream_bridge import AudioStreamBridge

RTP_VERSION = 2
PCMU_PAYLOAD_TYPE = 0
RTP_HEADER_FORMAT = "!BBHII"
RTP_HEADER_SIZE = struct.calcsize(RTP_HEADER_FORMAT)


@dataclass(slots=True)
class RtpSessionConfig:
    """Parameters that describe a unidirectional RTP stream."""

    local_host: str
    local_port: int
    remote_host: str
    remote_port: int
    payload_type: int = PCMU_PAYLOAD_TYPE


class _RtpReceiverProtocol(asyncio.DatagramProtocol):
    """Receives RTP packets and forwards audio payloads into the bridge."""

    def __init__(self, bridge: AudioStreamBridge, payload_type: int, logger: BoundLogger) -> None:
        self._bridge = bridge
        self._payload_type = payload_type
        self._logger = logger
        self._loop = asyncio.get_running_loop()
        self._pending: set[asyncio.Task[None]] = set()

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        if len(data) < RTP_HEADER_SIZE:
            self._logger.warning("rtp.packet_too_small", size=len(data))
            return
        header = data[:RTP_HEADER_SIZE]
        _, payload_marker, _, _, _ = struct.unpack(RTP_HEADER_FORMAT, header)
        payload_type = payload_marker & 0x7F
        if payload_type != self._payload_type:
            self._logger.debug("rtp.payload_type_mismatch", expected=self._payload_type, received=payload_type)
            return
        payload = data[RTP_HEADER_SIZE:]
        task = self._loop.create_task(self._bridge.enqueue_sip_audio(payload))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def error_received(self, exc: Exception) -> None:  # type: ignore[override]
        self._logger.warning("rtp.receiver_error", error=str(exc))

    async def close(self) -> None:
        for task in list(self._pending):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._pending.clear()


class RtpSession:
    """Bidirectional RTP helper tied to a single SIP dialog."""

    def __init__(self, bridge: AudioStreamBridge, config: RtpSessionConfig) -> None:
        self._bridge = bridge
        self._config = config
        self._logger: BoundLogger = structlog.get_logger(__name__).bind(
            remote=f"{config.remote_host}:{config.remote_port}",
            local_port=config.local_port,
        )
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._receiver: Optional[_RtpReceiverProtocol] = None
        self._sender_task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._sequence = random.randint(0, 65535)
        self._timestamp = random.randint(0, 2**31 - 1)
        self._ssrc = random.randint(0, 2**31 - 1)

    async def start(self) -> None:
        if self._running:
            return
        loop = asyncio.get_running_loop()
        self._receiver = _RtpReceiverProtocol(self._bridge, self._config.payload_type, self._logger)
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: self._receiver,
            local_addr=(self._config.local_host, self._config.local_port),
        )
        self._running = True
        self._sender_task = loop.create_task(self._sender_loop())
        self._logger.info("rtp.session_started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._sender_task:
            self._sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender_task
            self._sender_task = None
        if self._receiver:
            await self._receiver.close()
            self._receiver = None
        if self._transport:
            self._transport.close()
            self._transport = None
        self._logger.info("rtp.session_stopped")

    async def _sender_loop(self) -> None:
        assert self._transport is not None
        remote = (self._config.remote_host, self._config.remote_port)
        while self._running:
            payload = await self._bridge.dequeue_sip_audio()
            print(f"RTP Sending payload of size {len(payload)} to {remote}")
            header = self._build_header(len(payload))
            self._transport.sendto(header + payload, remote)
            self._sequence = (self._sequence + 1) % 65536
            self._timestamp = (self._timestamp + len(payload)) % (2**32)

    def _build_header(self, payload_length: int) -> bytes:
        marker_payload = self._config.payload_type | (0 << 7)
        return struct.pack(
            RTP_HEADER_FORMAT,
            (RTP_VERSION << 6),
            marker_payload,
            self._sequence,
            self._timestamp,
            self._ssrc,
        )
