"""Fastnet input sources: a live serial port or a captured hex file.

Fastnet line settings are 28800 baud, 8 data bits, 2 stop bits, odd parity.
Ported from fastnet2ip/core/input.py.
"""

import logging

import serial

BAUDRATE        = 28800
BYTE_SIZE       = serial.EIGHTBITS
STOP_BITS       = serial.STOPBITS_TWO
PARITY          = serial.PARITY_ODD
READ_SIZE       = 256

# Wire bits per transmitted byte: start + 8 data + parity + 2 stop.
BITS_PER_BYTE   = 1 + 8 + 1 + 2

# Pace --file replay at the real Fastnet line speed, so a replay runs at about the
# rate live serial does: BAUDRATE / BITS_PER_BYTE bytes per second, one READ_SIZE
# chunk per tick.
FILE_READ_DELAY = READ_SIZE / (BAUDRATE / BITS_PER_BYTE)   # ≈ 0.107 s per 256-byte chunk

logger = logging.getLogger("fastnet2n2k.input")


def open_serial_port(device):
    """Open the Fastnet serial port: 28800 baud, 8 data bits, odd parity, 2 stop.

    ``timeout=0`` makes reads non-blocking — ``read()`` returns whatever is buffered
    right now, or empty, and never waits. That is what lets SerialReader run on the
    event loop without holding it up.

    Raises ``serial.SerialException`` (a kind of ``OSError``) if the port won't open.
    """
    logger.info("Serial port: %s", device)
    ser = serial.Serial(port=device, baudrate=BAUDRATE, bytesize=BYTE_SIZE,
                        stopbits=STOP_BITS, parity=PARITY, timeout=0)
    _force_baudrate(ser)
    return ser


def _force_baudrate(ser):
    """Make the kernel actually program the baud rate into the UART.

    Do not remove this, and do not "simplify" it to a single assignment — it looks
    redundant and is not. Without it, the FIRST open of the port after a boot leaves
    the hardware running at 9600 while termios cheerfully reports 28800. Roughly two
    thirds of the bytes are then lost and the rest are garbage, so nothing decodes and
    the bridge sits there looking healthy while transmitting nothing. Every later open
    is fine, which is why "just run it a second time" appeared to fix it for months.

    Why it happens: 28800 is not a standard rate on Linux (there is no ``B28800``), so
    pyserial sets it through the ``BOTHER`` custom-divisor path — ``tcsetattr`` with
    ``CBAUD = BOTHER``, then the literal rate via a ``TCSETS2`` ioctl. The kernel's
    ``uart_set_termios()`` skips reprogramming the hardware when nothing "relevant"
    changed, and "relevant" means the ``c_cflag`` baud bits, not ``c_ispeed`` /
    ``c_ospeed``. Under ``BOTHER`` those bits are identical either side of the change,
    so the new speed is optimised away and never reaches the divisor; serial core then
    falls back to its 9600 default.

    The cure is one genuine ``c_cflag`` change. Bouncing to a *standard* rate and back
    alters the CBAUD bits twice, so the divisor really is written. Re-assigning 28800
    on its own does nothing at all — pyserial skips a value that hasn't changed, so
    there is no ioctl and no reprogramming.

    Measured on a CM4 (``/dev/ttyAMA5``) over six cold boots: without this, the first
    open runs at 34% of wire speed and decodes nothing; with it, 99.8% and clean.
    A standard rate such as 38400 is unaffected, which is what pins the cause on the
    custom-divisor path rather than on the board or the wiring.
    See ``docs/uart_first_open_baud_fix.md``.
    """
    ser.baudrate = 9600        # any standard rate — this is what moves the CBAUD bits
    ser.baudrate = BAUDRATE    # ...and back, which now actually programs the divisor


def load_capture_file(path):
    """Read a hex capture from disk and return it as an iterator of byte chunks.

    The file is hex text — spaces and newlines are ignored, so a capture written one
    frame per line works as-is. Chunking it to READ_SIZE makes a replay arrive in the
    same size pieces a real port delivers.

    Raises ``OSError`` if the file can't be read, or ``ValueError`` if it isn't hex.
    """
    logger.info("File: %s", path)
    with open(path) as f:
        hex_data = f.read().strip().replace(" ", "").replace("\n", "")
    if not hex_data:
        raise ValueError("File is empty")
    binary = bytes.fromhex(hex_data)
    return iter([binary[i:i + READ_SIZE] for i in range(0, len(binary), READ_SIZE)])


class SerialReader:
    """Feed a queue from a live serial port, driven by the event loop.

    ``add_reader`` wakes the loop the moment bytes arrive, and the read happens on
    the loop itself — single-threaded, so reads never overlap and byte order is
    preserved.
    """

    def __init__(self, loop, ser, queue):
        self._loop = loop
        self._ser = ser
        self._queue = queue
        self._fd = None

    def start(self):
        self._fd = self._ser.fileno()
        self._loop.add_reader(self._fd, self._drain)

    def stop(self):
        """Detach the fd from the loop and close the port. Safe to call twice."""
        if self._fd is not None:
            self._loop.remove_reader(self._fd)
            self._fd = None
        self._ser.close()

    def _drain(self):
        """Queue everything buffered on the port — non-blocking, and loops until the
        port comes back empty so a burst bigger than READ_SIZE is taken in one wakeup
        rather than one chunk per loop iteration."""
        try:
            while True:
                chunk = self._ser.read(READ_SIZE)
                if not chunk:
                    break            # port is empty for now; wait for the next wakeup
                self._queue.put_nowait(chunk)
        except (OSError, serial.SerialException) as exc:
            # Nothing here can recover a broken fd, and add_reader would keep calling
            # us in a tight loop on it, so stop listening and say so once.
            logger.error("Serial read failed on %s: %s — no longer reading",
                         self._ser.port, exc)
            self.stop()
