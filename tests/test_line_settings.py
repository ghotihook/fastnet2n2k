"""Line-settings read-back.

These are diagnostics used while a port is misbehaving, so the bar is that they
never raise — a broken fd or a platform without TCGETS2 must degrade to a string,
not take the bridge down with it.
"""

import os
import pty

from fastnet2n2k import line_settings


class _Port:
    """Enough of serial.Serial for the read-back helpers."""

    def __init__(self, fd, port="/dev/fake"):
        self.port = port
        self._fd = fd

    def fileno(self):
        if self._fd is None:
            raise OSError("no such fd")
        return self._fd


def test_tcgets2_matches_the_kernel_ioctl_number():
    """_IOR('T', 0x2A, struct termios2). If the struct size drifts, the ioctl number
    silently changes and the speed read-back starts failing."""
    assert line_settings._TERMIOS2.size == 44
    assert line_settings.TCGETS2 == 0x802C542A


def test_describe_degrades_instead_of_raising():
    assert "unavailable" in line_settings.describe(_Port(None))


def test_mismatch_degrades_to_none():
    assert line_settings.mismatch(_Port(None), 28800, 8, True, 2) is None


def test_describe_reads_a_real_tty():
    """A fresh pty is 8N1, so the cflag decode has a known answer."""
    master, slave = pty.openpty()
    try:
        described = line_settings.describe(_Port(slave))
        assert "8 data" in described
        assert "no parity" in described
        assert "1 stop" in described
    finally:
        os.close(master)
        os.close(slave)


def test_mismatch_names_every_wrong_field():
    """The point of the helper: say precisely how the port differs from the request."""
    master, slave = pty.openpty()
    try:
        wrong = line_settings.mismatch(_Port(slave), 28800, 8, True, 2)
        assert "parity" in wrong
        assert "stop bits" in wrong
    finally:
        os.close(master)
        os.close(slave)
