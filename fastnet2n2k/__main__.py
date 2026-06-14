"""fastnet2n2k entry point: Fastnet (serial/file) → decode → NMEA2000 on the CAN bus.

    python -m fastnet2n2k --serial /dev/ttyUSB0 --channel can0
    python -m fastnet2n2k --file capture.txt    --channel can0

Bring the CAN interface up first:
    sudo ip link set can0 up type can bitrate 250000
"""

import argparse
import logging
import sys

import can
import n2k
from fastnet_decoder import FrameBuffer

from . import mapping
from .input_source import initialize_input_source, read_input_source
from .live_store import live_data, update_live_data

logger = logging.getLogger("fastnet2n2k")

# PGNs this node transmits — advertised in its 126464 PGN-list response.
_TX_PGNS = [
    127245, 127250, 127251, 127257, 127508, 128000, 128259, 128267,
    128275, 129025, 129026, 129283, 130306, 130312, 130314,
]


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
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return p.parse_args()


def fnv_unique() -> int:
    """A stable-ish 21-bit unique number derived from the hostname, so two boards
    don't both default to the same NMEA2000 NAME."""
    import socket
    h = 2166136261
    for b in socket.gethostname().encode():
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h & 0x1FFFFF


def make_node(bus: can.BusABC, src: int, unique: int) -> n2k.Node:
    dev = n2k.DeviceInformation(
        unique_number=unique,
        manufacturer_code=2046,   # open-source / unregistered
        device_function=190,      # 190 = Navigation / Bridge
        device_class=60,          # 60 = Navigation
        industry_group=4,         # 4 = Marine
    )
    node = n2k.Node(bus, dev)
    node.n2k_source = src
    node.set_product_information("fastnet2n2k", "0.1.0", "POC", "00000000001", 2)
    node.set_configuration_information()
    node.transmit_messages.extend(_TX_PGNS)
    return node


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)-5s %(message)s")

    try:
        bus = can.Bus(args.channel, interface="socketcan")
    except OSError as exc:
        logger.error("Could not open CAN interface '%s': %s", args.channel, exc)
        logger.error("Bring it up first: sudo ip link set %s up type can bitrate 250000",
                     args.channel)
        return 1

    notifier = can.Notifier(bus, [])
    node = make_node(bus, args.n2k_src, args.unique)
    notifier.add_listener(node)
    mapping.set_node(node)
    logger.info("Transmitting on %s (src=%d); reading Fastnet from %s",
                args.channel, args.n2k_src, args.serial or args.file)

    source, is_file = initialize_input_source(serial_port=args.serial, file_path=args.file)
    fb = FrameBuffer()
    try:
        while True:
            data = read_input_source(source, is_file)
            if data is None:
                if is_file:
                    logger.info("File replay complete")
                    break
                continue
            fb.add_to_buffer(data)
            fb.get_complete_frames()
            while not fb.frame_queue.empty():
                _dispatch_frame(fb.frame_queue.get())
    except KeyboardInterrupt:
        logger.info("Stopping")
    finally:
        notifier.stop()
        bus.shutdown()
        if not is_file:
            source.close()
    return 0


def _dispatch_frame(frame: dict) -> None:
    """Update the live store for each decoded channel, then trigger its N2K frame."""
    for channel_name, decoded in frame.get("values", {}).items():
        old = live_data.get(channel_name)
        old_copy = dict(old) if old else None
        update_live_data(channel_name, decoded.get("channel_id"), decoded.get("value"),
                         decoded.get("display_text"), decoded.get("layout"))
        mapping.process_channel(channel_name, old_copy)


if __name__ == "__main__":
    sys.exit(main())
