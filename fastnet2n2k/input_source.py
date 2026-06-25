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

# Pace --file replay to roughly emulate the serial line: at ~38400 baud (~3840 B/s
# for a 10-bit byte) a READ_SIZE chunk takes ~0.067 s. Approximate is close enough.
FILE_READ_DELAY = READ_SIZE / 3840   # ≈ 0.067 s per 256-byte chunk (~38400 baud)

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


def attach_serial_reader(loop, ser, queue):
    """Register ``ser``'s fd with the asyncio event loop so it wakes only when bytes
    are available, pushing each chunk onto ``queue``. Returns the fd so the caller can
    ``loop.remove_reader(fd)`` on shutdown.

    Replaces the old thread-per-read model: the port is opened non-blocking
    (``timeout=0``), so ``read`` in the callback returns immediately with whatever is
    buffered, and there is no ThreadPoolExecutor handoff per chunk.
    """
    fd = ser.fileno()

    def _on_readable():
        try:
            chunk = ser.read(READ_SIZE)
        except (OSError, serial.SerialException) as exc:
            logger.error("Serial read error: %s", exc)
            return
        if chunk:
            queue.put_nowait(chunk)

    loop.add_reader(fd, _on_readable)
    return fd
