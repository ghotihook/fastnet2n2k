#!/usr/bin/env python3
"""Minimal NMEA2000 transmit POC for the M5Stack CoreMP135 (and any SocketCAN box).

Sends PGN 127250 (Vessel Heading) onto a CAN/NMEA2000 network via SocketCAN, using
the ``nmea2000`` library's N2KDevice (canboat-based; handles ISO address claiming).
Bring the bus up first::

    sudo ip link set can0 up type can bitrate 250000

Then::

    python nmea2000_poc.py --channel can0 --heading 90        # ~10 Hz loop
    python nmea2000_poc.py --channel can0 --heading 90 --once  # single frame
    python nmea2000_poc.py --channel can0 --rate 0             # unthrottled (find ceiling)
    python nmea2000_poc.py --channel can0 --cycle --rate 0     # sweep 0–359° as fast as possible

Prints the actual achieved rate once per second so the real on-bus output can be read
directly, independent of any downstream gateway/monitor.
"""

import argparse
import asyncio
import sys
import time
from math import radians

import can
import nmea2000.pgns as pgns
from nmea2000.device import N2KDevice


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel", default="can0", help="SocketCAN interface (default: can0)")
    p.add_argument("--heading", type=float, default=0.0, help="Heading in degrees (default: 0)")
    p.add_argument("--cycle", action="store_true",
                   help="Sweep heading through 0–359° (1° per frame, wrapping) starting "
                        "from --heading, instead of a fixed value; pair with --rate 0 "
                        "to cycle as fast as possible")
    p.add_argument("--ref", choices=("true", "magnetic"), default="true",
                   help="Heading reference (default: true)")
    p.add_argument("--rate", type=float, default=10.0,
                   help="Transmit rate in Hz; 0 = unthrottled (default: 10)")
    p.add_argument("--src", type=lambda x: int(x, 0), default=22,
                   help="Preferred N2K source address (default: 22)")
    p.add_argument("--once", action="store_true", help="Send a single frame and exit")
    return p.parse_args()


def build_heading(heading_deg: float, ref: str, sid: int):
    msg = pgns.decode_pgn_127250(0, 0)
    msg.source = 0
    msg.priority = 2
    # Deviation and variation are sent as "not available" (None → all-1s sentinel),
    # not 0. We have no source for them, and a 0 variation on a magnetic heading
    # would falsely assert magnetic == true.
    values = {"sid": sid, "heading": radians(heading_deg),
              "reference": "Magnetic" if ref == "magnetic" else "True",
              "deviation": None, "variation": None}
    for f in msg.fields:
        if f.id in values:
            f.raw_value = None
            f.value = values[f.id]
    return msg


async def run(args: argparse.Namespace) -> int:
    device = N2KDevice.for_python_can(
        "socketcan", args.channel, preferred_address=args.src,
        model_id="fastnet2n2k-poc", transmit_pgns=[127250])
    try:
        await device.start()
    except (OSError, can.CanError) as exc:
        print(f"Could not open CAN interface '{args.channel}': {exc}", file=sys.stderr)
        print(f"Bring it up first: sudo ip link set {args.channel} up type can bitrate 250000",
              file=sys.stderr)
        return 1

    try:
        await asyncio.wait_for(device.wait_ready(), timeout=10)
    except asyncio.TimeoutError:
        print("Address claim not confirmed within 10s — continuing", file=sys.stderr)

    period = 1.0 / args.rate if args.rate > 0 else 0.0
    sid = 0
    heading_deg = args.heading
    heading_desc = f"{args.heading:g}–359° cycling" if args.cycle else f"{args.heading:g}°"
    print(f"Sending PGN 127250 Heading={heading_desc} ({args.ref}) on {args.channel}"
          + (" once" if args.once else f" at {args.rate:g} Hz")
          + f"  src={device.address}  (Ctrl-C to stop)")

    next_send = time.monotonic()
    sent_in_window = 0
    window_start = time.monotonic()
    last_error = 0.0
    try:
        while True:
            try:
                await device.send(build_heading(heading_deg, args.ref, sid))
                sent_in_window += 1
            except Exception as exc:   # noqa: BLE001 — bus-off / interface drop
                if time.monotonic() - last_error >= 5.0:
                    print(f"  send failed ({exc}) — continuing; device will reconnect",
                          file=sys.stderr, flush=True)
                    last_error = time.monotonic()
            sid = (sid + 1) % 253
            if args.cycle:
                heading_deg = (heading_deg + 1) % 360
            if args.once:
                break
            now = time.monotonic()
            if now - window_start >= 1.0:
                measured = sent_in_window / (now - window_start)
                print(f"  measured {measured:7.1f} Hz  (target "
                      + (f"{args.rate:g}" if args.rate > 0 else "max") + ")", flush=True)
                sent_in_window = 0
                window_start = now
            next_send += period
            delay = next_send - now
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -period:
                next_send = time.monotonic()   # long stall (>1 period) — give up catching up
            # else: slightly late — send now, keep the grid so the average rate holds
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        await device.close()
    return 0


def main() -> int:
    try:
        return asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
