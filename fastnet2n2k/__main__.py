"""fastnet2n2k entry point: Fastnet (serial/file) → decode → NMEA2000 on the CAN bus.

    python -m fastnet2n2k --serial /dev/ttyUSB0 --channel can0
    python -m fastnet2n2k --file capture.txt    --channel can0

Bring the CAN interface up first:
    sudo ip link set can0 up type can bitrate 250000
"""

import argparse
import asyncio
import logging
import socket
import sys
import time

import can
from fastnet_decoder import FrameBuffer
from nmea2000.device import N2KDevice

from . import __version__, mapping
from .display import print_live_data
from .input_source import initialize_input_source, read_input_source
from .live_store import live_data, update_live_data

logger = logging.getLogger("fastnet2n2k")


class _QuietTransientCanErrors(logging.Filter):
    """Drop the nmea2000 client's per-failure spam when the CAN transmit buffer is
    full (ENOBUFS / error 105) — including the ``exc_info`` tracebacks it logs.

    These fire on every frame when the bus can't drain (e.g. no other node is
    acknowledging), flooding the journal. The library already retries internally,
    and ``mapping.process_channel`` logs a single throttled summary when a send
    ultimately fails, so the per-attempt warnings add only noise. Genuine
    connection-lost errors are logged at ERROR and pass through untouched.
    """

    _NOISE = ("transmit queue full", "send failed without reconnecting")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage().lower()
        return not any(phrase in msg for phrase in self._NOISE)


def fnv_unique() -> int:
    """A stable 21-bit unique number derived from the hostname, so two boards don't
    default to the same NMEA2000 NAME."""
    h = 2166136261
    for b in socket.gethostname().encode():
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h & 0x1FFFFF


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--serial", metavar="DEV", help="Fastnet serial port, e.g. /dev/ttyUSB0")
    src.add_argument("--file", metavar="PATH", help="Captured Fastnet hex (.txt) to replay")
    p.add_argument("--channel", default="can0", help="SocketCAN interface (default: can0)")
    p.add_argument("--n2k-priority", type=lambda x: int(x, 0), default=None,
                   help="Override the CAN priority (0–7, 0=highest) for ALL transmitted "
                        "frames. Default: each PGN uses its NMEA2000 standard priority.")
    p.add_argument("--unique", type=int, default=fnv_unique(),
                   help="Device NAME unique number (default: derived from hostname)")
    p.add_argument("--live-data", action="store_true",
                   help="Print the live channel table to the console once per second")
    p.add_argument("--log-level", default="INFO",
                   choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                   help="Logging verbosity (default: INFO)")
    args = p.parse_args()
    if args.n2k_priority is not None and not 0 <= args.n2k_priority <= 7:
        p.error("--n2k-priority must be 0–7")
    return args


def make_device(args: argparse.Namespace) -> N2KDevice:
    return N2KDevice.for_python_can(
        "socketcan", args.channel,
        unique_number=args.unique,
        manufacturer_code=2046,    # open-source / unregistered
        device_function=190,       # 190 = Navigation
        device_class=60,           # 60 = Navigation
        industry_group=4,          # 4 = Marine
        model_id="fastnet2n2k",
        model_version=__version__,
        software_version_code=__version__,
        transmit_pgns=mapping.TX_PGNS,
    )


async def _dispatch_frame(frame: dict) -> None:
    for channel_name, decoded in frame.get("values", {}).items():
        old = live_data.get(channel_name)
        old_copy = dict(old) if old else None
        update_live_data(channel_name, decoded.get("channel_id"), decoded.get("value"),
                         decoded.get("display_text"), decoded.get("layout"))
        await mapping.process_channel(channel_name, old_copy)


async def run(args: argparse.Namespace) -> int:
    try:
        device = make_device(args)
    except (OSError, can.CanError) as exc:
        logger.error("Could not create CAN interface '%s': %s", args.channel, exc)
        logger.error("Bring it up first: sudo ip link set %s up type can bitrate 250000",
                     args.channel)
        return 1

    # connect() retries with backoff until the interface is available, so this waits
    # (rather than failing) if can0 isn't up yet — the right behaviour for a boat bus
    # that may power-cycle. Watch the logs; raise --log-level if it seems stuck.
    logger.info("Connecting to CAN interface %s (waiting if it isn't up yet)…",
                args.channel)
    await device.start()

    mapping.set_device(device)
    if args.n2k_priority is not None:
        mapping.set_priority_override(args.n2k_priority)
        logger.info("Overriding priority for all frames: %d", args.n2k_priority)
    logger.info("Transmitting on %s (src=%d); reading Fastnet from %s",
                args.channel, device.address, args.serial or args.file)
    try:
        await asyncio.wait_for(device.wait_ready(), timeout=10)
        logger.info("Address claimed: %d", device.address)
    except asyncio.TimeoutError:
        logger.warning("Address claim not confirmed within 10s — continuing")

    source, is_file = initialize_input_source(serial_port=args.serial, file_path=args.file)
    fb = FrameBuffer()
    last_print = time.monotonic()
    try:
        while True:
            data = await asyncio.to_thread(read_input_source, source, is_file)
            if data is not None:
                fb.add_to_buffer(data)
                fb.get_complete_frames()
                while not fb.frame_queue.empty():
                    await _dispatch_frame(fb.frame_queue.get())

            if args.live_data and time.monotonic() - last_print >= 1:
                print_live_data(fb)
                last_print = time.monotonic()

            if is_file and data is None:
                logger.info("File replay complete")
                break
    except KeyboardInterrupt:
        logger.info("Stopping")
    finally:
        await device.close()
        if not is_file:
            source.close()
    return 0


def main() -> int:
    args = parse_args()
    level = getattr(logging, args.log_level)
    logging.basicConfig(
        level=level, format="%(asctime)s [%(name)s] %(levelname)-5s %(message)s")
    # The nmea2000 client is chatty at DEBUG; keep it in step with our level.
    logging.getLogger("nmea2000").setLevel(level)
    # …but drop its transmit-buffer-full tracebacks; we summarise those ourselves.
    logging.getLogger("nmea2000.ioclient").addFilter(_QuietTransientCanErrors())
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
