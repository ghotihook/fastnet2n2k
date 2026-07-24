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

import can
from fastnet_decoder import FrameBuffer
from nmea2000.device import N2KDevice

from . import __version__, mapping
from .display import print_live_data
from .input_source import (
    FILE_READ_DELAY,
    SerialReader,
    load_capture_file,
    open_serial_port,
)
from .live_store import update_live_data

logger = logging.getLogger("fastnet2n2k")


class _QuietTransientCanErrors(logging.Filter):
    """Drop per-frame spam when the CAN transmit buffer is full (ENOBUFS): the
    nmea2000 library retries internally and ``mapping.process_channel`` logs one
    throttled summary, so the per-attempt warnings (with tracebacks) add only noise.
    Genuine connection-lost errors are logged at ERROR and pass through.

    Also drops any record that can't be rendered: at DEBUG, the library's seed
    messages carry a string timestamp that crashes ``can.Message.__str__``, which
    would otherwise print a ``--- Logging error ---`` traceback per seed send.
    Attached to the root handler because the bad record can come from either the
    nmea2000 or the python-can logger.
    """

    _NOISE = ("transmit queue full", "send failed without reconnecting")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage().lower()
        except Exception:   # unrenderable args (e.g. can.Message with a str timestamp)
            return False
        return not any(phrase in msg for phrase in self._NOISE)


def fnv_unique() -> int:
    """A stable 21-bit unique number derived from the hostname, so two boards don't
    default to the same NMEA2000 NAME.

    This is the standard FNV-1a hash: start from a fixed seed, then for each byte of
    the hostname XOR it in and multiply by a fixed prime, keeping the result 32-bit.
    Any hash would do — it just needs to be stable across reboots (so the device
    keeps its identity on the bus) and unlikely to collide between two boards.
    The final mask keeps the low 21 bits, which is the field width NMEA2000 allows.
    """
    FNV_OFFSET_BASIS = 2166136261
    FNV_PRIME = 16777619

    h = FNV_OFFSET_BASIS
    for byte in socket.gethostname().encode():
        h = ((h ^ byte) * FNV_PRIME) & 0xFFFFFFFF
    return h & 0x1FFFFF   # 21 bits


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
    p.add_argument("--dump-serial", metavar="PATH",
                   help="Append every received serial chunk as hex, for diagnosing a "
                        "stream that won't decode. The file replays with --file.")
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
    # pyfastnet v3 emits {signalk_path: SI_value}.
    for path, value in frame.get("values", {}).items():
        update_live_data(path, value)
        await mapping.process_channel(path)


async def _print_live_loop(fb) -> None:
    """Refresh the --live-data table once a second from its own task, so the read loop
    stays free of any per-chunk timer."""
    while True:
        await asyncio.sleep(1)
        print_live_data(fb)


async def run(args: argparse.Namespace) -> int:
    """Set up the CAN device and the Fastnet input, then pump one into the other
    until interrupted (or, for ``--file``, until the capture runs out).

    Startup order matters: the CAN device has to be connected and have claimed an
    address before there is any point reading Fastnet, because frames decoded before
    then would have nowhere to go.
    """
    logger.info("fastnet2n2k %s", __version__)
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

    # --serial and --file are mutually exclusive and one is required, so exactly one
    # of these runs. Either way `source` is where raw Fastnet bytes come from.
    is_file = args.file is not None
    try:
        source = load_capture_file(args.file) if is_file else open_serial_port(args.serial)
    except (OSError, ValueError) as exc:
        logger.error("Cannot read Fastnet input: %s", exc)
        return 1

    fb = FrameBuffer()
    loop = asyncio.get_running_loop()

    # Live serial is event-driven on the loop (no per-read thread); a file is paced
    # on the loop directly.
    reader = None
    queue: asyncio.Queue = asyncio.Queue()
    if not is_file:
        reader = SerialReader(loop, source, queue)
        reader.start()

    # Line-buffered so a capture survives Ctrl-C or a kill. Chunks go out as hex,
    # the format load_capture_file() reads, so a dump replays with --file.
    dump = open(args.dump_serial, "a", buffering=1) if args.dump_serial else None
    if dump is not None:
        logger.info("Dumping raw serial to %s", args.dump_serial)

    printer = asyncio.create_task(_print_live_loop(fb)) if args.live_data else None
    try:
        # The pipeline, one chunk of raw bytes at a time:
        #   read bytes -> FrameBuffer assembles whole Fastnet frames -> each frame's
        #   values go to the live store -> mapping turns them into PGNs and transmits.
        while True:
            if is_file:
                await asyncio.sleep(FILE_READ_DELAY)   # pace the replay at wire speed
                data = next(source, None)              # None once the file runs out
                if data is None:
                    logger.info("File replay complete")
                    break
            else:
                data = await queue.get()   # SerialReader puts bytes here as they arrive

            if dump is not None:
                dump.write(data.hex() + "\n")
            fb.add_to_buffer(data)
            fb.get_complete_frames()
            while not fb.frame_queue.empty():
                await _dispatch_frame(fb.frame_queue.get())
    finally:
        if printer is not None:
            printer.cancel()
        if reader is not None:
            reader.stop()   # detaches the fd, closes the port
        if dump is not None:
            dump.close()
        await device.close()
    return 0


def main() -> int:
    args = parse_args()
    level = getattr(logging, args.log_level)
    logging.basicConfig(
        level=level, format="%(asctime)s [%(name)s] %(levelname)-5s %(message)s")
    # The nmea2000 client is chatty at DEBUG; keep it in step with our level.
    logging.getLogger("nmea2000").setLevel(level)
    # pyfastnet pins its own logger to INFO and attaches its own handler, so its
    # per-frame BUF / FRAME discard / QUEUE lines were unreachable from the CLI —
    # exactly the evidence needed when a stream arrives but won't decode. Drop the
    # library handler so its records come through ours, formatted and filtered.
    _pyfastnet = logging.getLogger("pyfastnet")
    _pyfastnet.handlers.clear()
    _pyfastnet.setLevel(level)
    # Drop transmit-buffer-full spam (we summarise that ourselves) and any record that
    # can't be rendered — e.g. python-can stringifying a seed message with a string
    # timestamp. Attached to the root handler so it covers every logger, not just
    # nmea2000.ioclient (the broken seed-send record comes from python-can's own logger).
    _quiet = _QuietTransientCanErrors()
    for _handler in logging.getLogger().handlers:
        _handler.addFilter(_quiet)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
