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
