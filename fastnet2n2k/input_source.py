"""Fastnet input sources: a live serial port or a captured hex file.

Fastnet line settings are 28800 baud, 8 data bits, 2 stop bits, odd parity.
Ported from fastnet2ip/core/input.py.
"""

import asyncio
import logging
import time

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


# Safety-net poll rate. The add_reader fast path handles reads with ~zero latency when
# it fires; this only backstops a missed wakeup, so it need not be fast — 10 Hz bounds
# worst-case read latency to ~100 ms (well under any consumer's PGN timeout) while
# costing a single non-blocking read per tick (no thread handoff), negligible on the SBC.
SAFETY_POLL_INTERVAL = 0.1

# Silence threshold before the port is closed and reopened. Fastnet chatter is
# continuous while the instruments are powered, so a port silent this long is either
# on a bus that's off (reopening is then harmless) or has dead RX — which the Pi UART
# exhibits on its first open after a cold boot: no bytes ever reach the fd no matter
# how it is polled, and only a close/reopen restores reception. (Hardware-verified
# fix for the "works only on the second run" bug; polling alone cannot cure it.)
SILENCE_REOPEN_INTERVAL = 5.0

# Second reopen trigger: bytes arriving that never decode. A misconfigured port (wrong
# divisor or parity — 28800 is not a standard baud, so pyserial sets it through the
# BOTHER/TCSETS2 custom-divisor path) delivers a continuous stream the frame decoder
# can never checksum, which the silence watchdog cannot see because the port is not
# silent. Fastnet broadcast frames arrive many times a second, so a gap this long with
# bytes still flowing means the stream is undecodable. Longer than
# SILENCE_REOPEN_INTERVAL so a genuinely dead port trips the more specific path first.
NO_FRAME_REOPEN_INTERVAL = 10.0


class SerialReader:
    """Drive a live serial port and keep it alive.

    Three cooperating mechanisms, all on the event loop (single-threaded, so reads
    never overlap and byte order is preserved):

    - ``add_reader`` fast path: the fd wakes the loop the moment bytes arrive.
    - 10 Hz safety poll: drains the port even if the readable notification is missed.
    - Reopen watchdog: close and reopen the port when it stops making progress —
      either no bytes at all for ``SILENCE_REOPEN_INTERVAL``, or bytes that never
      decode into a frame for ``NO_FRAME_REOPEN_INTERVAL``. Both are first-boot
      failure modes of the Pi UART that no amount of polling can cure; the reopen
      automates the "stop it and run it again" workaround.
    """

    def __init__(self, loop, ser, queue):
        self._loop = loop
        self._ser = ser
        self._queue = queue
        self._fd = None
        self._poll_task = None
        now = time.monotonic()
        self._last_rx = now
        self._last_frame = now
        self._reopens = 0

    def start(self):
        """Attach the fd to the loop and start the poll/watchdog task."""
        self._attach()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        """Cancel the poll task, detach the fd and close the port."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._detach()
        self._ser.close()

    def _attach(self):
        self._fd = self._ser.fileno()
        self._loop.add_reader(self._fd, self._drain)

    def _detach(self):
        if self._fd is not None:
            self._loop.remove_reader(self._fd)
            self._fd = None

    def note_frame(self):
        """Called by the read loop for every decoded frame.

        A decoded frame is the only proof the port is genuinely working: bytes alone
        only show it is electrically alive, and a misconfigured port delivers plenty
        of those. So this both feeds the no-frame watchdog and reports recovery.
        """
        self._last_frame = time.monotonic()
        if self._reopens:
            logger.info("Serial data decoding again after %d reopen(s)", self._reopens)
            self._reopens = 0

    def _drain(self):
        """Queue everything buffered on the port (non-blocking, loops until empty —
        a single READ_SIZE read per tick could fall behind the ~2880 B/s line rate
        if the fast path ever stopped firing)."""
        got_data = False
        try:
            while chunk := self._ser.read(READ_SIZE):
                self._queue.put_nowait(chunk)
                got_data = True
        except (OSError, serial.SerialException) as exc:
            # Not per-tick spam-worthy: a dead port stays silent, so the watchdog
            # reopens it (with a WARNING) after the silence threshold.
            logger.debug("Serial read error (watchdog will reopen): %s", exc)
        if got_data:
            self._last_rx = time.monotonic()

    async def _poll_loop(self):
        while True:
            await asyncio.sleep(SAFETY_POLL_INTERVAL)
            self._drain()
            now = time.monotonic()
            if now - self._last_rx >= SILENCE_REOPEN_INTERVAL:
                self._reopen("no data for %.0fs" % SILENCE_REOPEN_INTERVAL)
            elif now - self._last_frame >= NO_FRAME_REOPEN_INTERVAL:
                self._reopen("data arriving but nothing decodable for %.0fs"
                             % NO_FRAME_REOPEN_INTERVAL)

    def _reopen(self, reason):
        """Close and reopen a port that has stopped making progress, re-registering
        the (new) fd. ``reason`` names which watchdog tripped."""
        self._reopens += 1
        # First reopen at WARNING so a genuine fault is visible; repeats (e.g. the
        # instruments are simply switched off) drop to DEBUG to keep the journal quiet.
        level = logging.WARNING if self._reopens == 1 else logging.DEBUG
        logger.log(level, "Reopening %s — %s (attempt %d)",
                   self._ser.port, reason, self._reopens)
        self._detach()
        try:
            self._ser.close()
            self._ser.open()
        except (OSError, serial.SerialException) as exc:
            logger.error("Reopen of %s failed: %s — retrying in %.0fs",
                         self._ser.port, exc, SILENCE_REOPEN_INTERVAL)
            self._reset_clocks()   # try again after the interval, not on the next tick
            return
        self._attach()
        self._reset_clocks()

    def _reset_clocks(self):
        """Restart both watchdog clocks. Both, always — leaving the no-frame clock
        stale after a silence reopen would trip it again on the very next tick."""
        now = time.monotonic()
        self._last_rx = now
        self._last_frame = now
