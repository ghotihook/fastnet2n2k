"""Fastnet input sources: a live serial port or a captured hex file.

Fastnet line settings are 28800 baud, 8 data bits, 2 stop bits, odd parity.
Ported from fastnet2ip/core/input.py.
"""

import logging
import select
import time

import serial

BAUDRATE        = 28800
BYTE_SIZE       = serial.EIGHTBITS
STOP_BITS       = serial.STOPBITS_TWO
PARITY          = serial.PARITY_ODD
READ_SIZE       = 256
FILE_READ_DELAY = 0.05

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


def read_input_source(input_source, is_file):
    """Return the next chunk of bytes, or ``None`` (file exhausted / no data yet)."""
    if is_file:
        try:
            time.sleep(FILE_READ_DELAY)
            return next(input_source)
        except StopIteration:
            return None
    rlist, _, _ = select.select([input_source], [], [], 1)
    if input_source in rlist:
        return input_source.read(READ_SIZE)
    return None
