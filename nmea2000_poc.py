#!/usr/bin/env python3
"""Minimal NMEA2000 transmit POC for the M5Stack CoreMP135.

Sends a PGN 127250 (Vessel Heading) message onto a CAN/NMEA2000 network via
SocketCAN. The CoreMP135 runs Linux and exposes its two FDCAN interfaces as the
SocketCAN netdevs ``can0`` / ``can1``. Bring the bus up at the NMEA2000 bitrate
first::

    sudo ip link set can0 up type can bitrate 250000

Then::

    python nmea2000_poc.py --channel can0 --heading 90        # ~10 Hz loop
    python nmea2000_poc.py --channel can0 --heading 90 --once  # single frame

Message construction, 29-bit CAN-ID encoding, fast-packet framing and ISO
address claiming are all handled by the ``n2k`` library.
"""

import argparse
import sys
import time
from math import radians

import can
import n2k
from n2k.messages import Heading, create_n2k_heading_message

BITRATE_HINT = "sudo ip link set {ch} up type can bitrate 250000"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel", default="can0",
                   help="SocketCAN interface (default: can0 = FDCAN1)")
    p.add_argument("--heading", type=float, default=0.0,
                   help="Heading to send, in degrees (default: 0)")
    p.add_argument("--ref", choices=("true", "magnetic"), default="true",
                   help="Heading reference (default: true)")
    p.add_argument("--rate", type=float, default=10.0,
                   help="Transmit rate in Hz when looping (default: 10)")
    p.add_argument("--once", action="store_true",
                   help="Send a single frame and exit")
    return p.parse_args()


def make_node(bus: can.BusABC) -> n2k.Node:
    """Create the N2K node and publish its identity onto the bus."""
    device_information = n2k.DeviceInformation(
        unique_number=1,
        manufacturer_code=2046,   # 2046 = open-source / unregistered
        device_function=140,      # 140 = Heading Sensor
        device_class=60,          # 60 = Navigation
        industry_group=4,         # 4 = Marine
    )
    node = n2k.Node(bus, device_information)
    node.set_product_information(
        name="CoreMP135 N2K POC",
        firmware_version="0.0.1",
        model_version="POC",
        model_serial_code="00000000001",
        load_equivalency=2,
    )
    node.set_configuration_information()
    return node


def main() -> int:
    args = parse_args()
    ref = (n2k.types.N2kHeadingReference.true if args.ref == "true"
           else n2k.types.N2kHeadingReference.magnetic)

    try:
        bus = can.Bus(args.channel, interface="socketcan")
    except OSError as exc:
        print(f"Could not open CAN interface '{args.channel}': {exc}", file=sys.stderr)
        print(f"Bring the interface up first: {BITRATE_HINT.format(ch=args.channel)}",
              file=sys.stderr)
        return 1

    # The Notifier feeds received frames to the node so ISO address claiming works.
    notifier = can.Notifier(bus, [])
    node = make_node(bus)
    notifier.add_listener(node)

    period = 1.0 / args.rate if args.rate > 0 else 0.0
    sid = 0
    print(f"Sending PGN 127250 Heading={args.heading}° ({args.ref}) on "
          f"{args.channel}" + (" once" if args.once else f" at {args.rate} Hz")
          + "  (Ctrl-C to stop)")
    # Schedule against an absolute monotonic deadline so the send work and sleep
    # overshoot don't accumulate into rate drift.
    next_send = time.monotonic()
    # Measure the actual achieved send rate over a rolling window.
    REPORT_INTERVAL = 1.0
    sent_in_window = 0
    window_start = time.monotonic()
    try:
        while True:
            msg = create_n2k_heading_message(Heading(
                heading=radians(args.heading),
                deviation=None,
                variation=None,
                ref=ref,
                sid=sid,
            ))
            node.send_msg(msg)
            sid = (sid + 1) % 253      # SID wraps 0..252; 253-255 are reserved
            sent_in_window += 1
            if args.once:
                break
            now = time.monotonic()
            if now - window_start >= REPORT_INTERVAL:
                measured = sent_in_window / (now - window_start)
                print(f"  measured {measured:6.1f} Hz  (target "
                      + (f"{args.rate:g}" if args.rate > 0 else "max") + ")", flush=True)
                sent_in_window = 0
                window_start = now
            next_send += period
            delay = next_send - now
            if delay > 0:
                time.sleep(delay)
            elif delay < -period:
                next_send = time.monotonic()   # long stall (>1 period) — give up catching up
            # else: slightly late — send now and keep the grid so the average rate holds
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        notifier.stop()
        bus.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
