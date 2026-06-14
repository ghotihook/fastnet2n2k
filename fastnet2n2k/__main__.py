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

from . import mapping
from .display import print_live_data
from .input_source import initialize_input_source, read_input_source
from .live_store import live_data, update_live_data

logger = logging.getLogger("fastnet2n2k")


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
    p.add_argument("--n2k-src", type=lambda x: int(x, 0), default=22,
                   help="Preferred N2K source address 0–251 (default: 22)")
    p.add_argument("--unique", type=int, default=fnv_unique(),
                   help="Device NAME unique number (default: derived from hostname)")
    p.add_argument("--live-data", action="store_true",
                   help="Print the live channel table to the console once per second")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return p.parse_args()


def make_device(args: argparse.Namespace) -> N2KDevice:
    return N2KDevice.for_python_can(
        "socketcan", args.channel,
        preferred_address=args.n2k_src,
        unique_number=args.unique,
        manufacturer_code=2046,    # open-source / unregistered
        device_function=190,       # 190 = Navigation
        device_class=60,           # 60 = Navigation
        industry_group=4,          # 4 = Marine
        model_id="fastnet2n2k",
        model_version="0.1.0",
        software_version_code="0.1.0",
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
    device = make_device(args)
    try:
        await device.start()
    except (OSError, can.CanError) as exc:
        logger.error("Could not open CAN interface '%s': %s", args.channel, exc)
        logger.error("Bring it up first: sudo ip link set %s up type can bitrate 250000",
                     args.channel)
        return 1

    mapping.set_device(device)
    logger.info("Transmitting on %s (preferred src=%d); reading Fastnet from %s",
                args.channel, args.n2k_src, args.serial or args.file)
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
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)-5s %(message)s")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
