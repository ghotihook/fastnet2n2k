"""Fastnet input sources: a live serial port or a captured hex file.

Fastnet line settings are 28800 baud, 8 data bits, 2 stop bits, odd parity.
Ported from fastnet2ip/core/input.py.
"""

import asyncio
import logging

import serial

BAUDRATE        = 28800
BYTE_SIZE       = serial.EIGHTBITS
STOP_BITS       = serial.STOPBITS_TWO
PARITY          = serial.PARITY_ODD
READ_SIZE       = 256

# Pace --file replay to roughly the real Fastnet line speed (BAUDRATE), so a replay
# runs at about the same rate as live serial. At ~10 bits/byte that's BAUDRATE/10 B/s,
# and a READ_SIZE chunk takes that long. Approximate is close enough.
FILE_READ_DELAY = READ_SIZE / (BAUDRATE / 10)   # ≈ 0.089 s per 256-byte chunk (~28800 baud)

logger = logging.getLogger("fastnet2n2k.input")


def initialize_input_source(serial_port=None, file_path=None):
    """Return ``(source, is_file)``. ``source`` is a ``serial.Serial`` for a live
    port, or an iterator of byte chunks for a file."""
    if serial_port:
        logger.info("Serial port: %s", serial_port)
        try:
            return serial.Serial(
                port=serial_port, baudrate=BAUDRATE, bytesize=BYTE_SIZE,
                stopbits=STOP_BITS, parity=PARITY, timeout=0,
            ), False
        except (serial.SerialException, OSError) as e:
            logger.error("Cannot open %s: %s", serial_port, e)
            raise SystemExit(1)
    elif file_path:
        logger.info("File: %s", file_path)
        try:
            with open(file_path) as f:
                hex_data = f.read().strip().replace(" ", "").replace("\n", "")
            if not hex_data:
                raise ValueError("File is empty")
            binary = bytes.fromhex(hex_data)
        except (OSError, ValueError) as e:
            logger.error("File error: %s", e)
            raise SystemExit(1)
        chunks = [binary[i:i + READ_SIZE] for i in range(0, len(binary), READ_SIZE)]
        return iter(chunks), True
    else:
        logger.error("Specify a serial port or a file")
        raise SystemExit(1)


def _drain_port(ser, queue):
    """Read whatever is buffered on ``ser`` (non-blocking) and, if any, queue it.
    Safe to call from both the fd-readable callback and the safety-net poll: the loop
    is single-threaded, so the two never overlap and byte order is preserved."""
    try:
        chunk = ser.read(READ_SIZE)
    except (OSError, serial.SerialException) as exc:
        logger.error("Serial read error: %s", exc)
        return
    if chunk:
        queue.put_nowait(chunk)


def attach_serial_reader(loop, ser, queue):
    """Register ``ser``'s fd with the asyncio event loop so it wakes when bytes are
    available, pushing each chunk onto ``queue``. Returns the fd so the caller can
    ``loop.remove_reader(fd)`` on shutdown.

    Replaces the old thread-per-read model: the port is opened non-blocking
    (``timeout=0``), so ``read`` in the callback returns immediately with whatever is
    buffered, and there is no ThreadPoolExecutor handoff per chunk. Pair with
    :func:`serial_safety_poll` — on the Pi UART this readable notification can fail to
    wake the loop on the first-after-boot run, so the poll guarantees the port is
    drained regardless.
    """
    fd = ser.fileno()
    loop.add_reader(fd, lambda: _drain_port(ser, queue))
    return fd


# Safety-net poll rate. The add_reader fast path handles reads with ~zero latency when
# it fires; this only backstops a missed wakeup, so it need not be fast — 10 Hz bounds
# worst-case read latency to ~100 ms (well under any consumer's PGN timeout) while
# costing a single non-blocking read per tick (no thread handoff), negligible on the SBC.
SAFETY_POLL_INTERVAL = 0.1


async def serial_safety_poll(ser, queue, interval=SAFETY_POLL_INTERVAL):
    """Periodically drain the serial port even if ``add_reader`` never fires.

    On the Raspberry Pi UART the loop's fd-readiness notification can register but not
    wake ``select()`` on a cold-boot run; the loop then blocks forever on an empty
    queue and no Fastnet frames are ever read ("works only on the second run"). A
    steady low-rate poll makes reading reliable — it was the accidental effect of
    ``--live-data`` (its 1 Hz print task) and of ``--file`` mode's per-iteration sleep,
    both of which kept the loop ticking; this makes that guarantee explicit and
    always-on for live serial input.
    """
    while True:
        await asyncio.sleep(interval)
        _drain_port(ser, queue)
