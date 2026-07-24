"""Report the line settings the kernel actually applied to a serial port.

``serial.Serial(...)`` reports what we *asked* for; this reports what the tty
actually has, which is not the same question:

- 28800 is not a standard baud rate (it is absent from pyserial's
  ``BAUDRATE_CONSTANTS``), so it is configured through the ``BOTHER``/``TCSETS2``
  custom-divisor path rather than a plain ``cfsetspeed``. If that does not take, the
  port runs at whatever speed it was left at and delivers bytes that never decode.
- termios belongs to the **tty**, not to the fd. Any other process that opens the
  port can overwrite our settings underneath us.

Both would show up here as a mismatch between what we asked for and what came back.
Linux-only (``TCGETS2`` does not exist elsewhere); every call degrades to a "not
available" string rather than raising, so it is safe to call from anywhere.
"""

import fcntl
import struct
import termios

# struct termios2 {
#     tcflag_t c_iflag, c_oflag, c_cflag, c_lflag;   4 × unsigned int
#     cc_t     c_line;                               1 × unsigned char
#     cc_t     c_cc[19];                            19 × unsigned char
#     speed_t  c_ispeed, c_ospeed;                   2 × unsigned int
# };  -> 44 bytes, no padding needed (36 is already 4-byte aligned).
_TERMIOS2 = struct.Struct("=4IB19B2I")

# _IOR('T', 0x2A, struct termios2) — 0x802C542A on arm/arm64/x86.
TCGETS2 = (2 << 30) | (_TERMIOS2.size << 16) | (ord("T") << 8) | 0x2A

_CSIZE_BITS = {
    getattr(termios, name): int(name[2:]) for name in ("CS5", "CS6", "CS7", "CS8")
}


def _actual_speeds(fd):
    """``(ispeed, ospeed)`` as literal integers, or ``None``.

    ``termios.tcgetattr`` reports the ``BOTHER`` sentinel rather than the real rate
    for a custom divisor, so the raw ``TCGETS2`` struct is the only way to see it.
    """
    try:
        raw = fcntl.ioctl(fd, TCGETS2, bytes(_TERMIOS2.size))
        fields = _TERMIOS2.unpack(raw)
    except OSError:
        return None
    return fields[-2], fields[-1]


def describe(ser):
    """One-line summary of the port's real line settings, e.g.
    ``28800 baud (in 28800), 8 data, odd parity, 2 stop``.

    Returns a "not available" string rather than raising, on any platform or error.
    """
    try:
        fd = ser.fileno()
        cflag = termios.tcgetattr(fd)[2]
    except Exception as exc:            # noqa: BLE001 — diagnostics must never throw
        return f"line settings unavailable ({exc})"

    if not cflag & termios.PARENB:
        parity = "no"
    elif cflag & termios.PARODD:
        parity = "odd"
    else:
        parity = "even"

    bits = _CSIZE_BITS.get(cflag & termios.CSIZE, "?")
    stop = 2 if cflag & termios.CSTOPB else 1

    speeds = _actual_speeds(fd)
    if speeds is None:
        speed = "speed unknown"
    elif speeds[0] == speeds[1]:
        speed = f"{speeds[1]} baud"
    else:
        speed = f"{speeds[1]} baud (in {speeds[0]})"

    return f"{speed}, {bits} data, {parity} parity, {stop} stop"


def mismatch(ser, baudrate, bytesize, parity_odd, stopbits):
    """Return a description of how the port differs from what was requested, or
    ``None`` if it matches (or cannot be read). Used to log the difference loudly —
    a port that came up mis-set is the whole question."""
    try:
        fd = ser.fileno()
        cflag = termios.tcgetattr(fd)[2]
    except Exception:                   # noqa: BLE001
        return None

    wrong = []
    speeds = _actual_speeds(fd)
    if speeds is not None and speeds[1] != baudrate:
        wrong.append(f"baud {speeds[1]} != {baudrate}")
    if _CSIZE_BITS.get(cflag & termios.CSIZE) != bytesize:
        wrong.append(f"data bits != {bytesize}")
    if bool(cflag & termios.PARENB and cflag & termios.PARODD) != parity_odd:
        wrong.append("parity != odd" if parity_odd else "parity != expected")
    if bool(cflag & termios.CSTOPB) != (stopbits == 2):
        wrong.append(f"stop bits != {stopbits}")
    return ", ".join(wrong) or None
