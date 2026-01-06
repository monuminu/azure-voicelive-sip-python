"""Gateway entry point for the Python Voice Live SIP integration."""
from __future__ import annotations

import asyncio
import signal

from voicelive_sip_gateway.config.settings import load_settings
from voicelive_sip_gateway.logging.setup import configure_logging
from voicelive_sip_gateway.sip.agent import SipAgent


async def _serve() -> None:
    settings = load_settings()
    configure_logging(settings.logging.level, settings.logging.log_file)

    sip_agent = SipAgent(settings=settings)

    await sip_agent.start()

    stop_event = asyncio.Event()

    def _handle_signal(*_: int) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows may not support certain signals; best-effort only.
            pass

    await stop_event.wait()
    await sip_agent.stop()


def run() -> None:
    """Module entry point used by `python -m` and console script."""

    asyncio.run(_serve())


if __name__ == "__main__":
    run()
