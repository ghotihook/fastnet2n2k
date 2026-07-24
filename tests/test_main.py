"""Entry-point pieces that are pure enough to test cheaply.

Not the run() loop — that needs a live CAN device and event loop, and mocking it
into a permanent test would be brittle for little gain. These are the standalone
bits: the log filter (fragile, and load-bearing when the bus misbehaves), the NAME
hash (a stability contract), and the one argument that has a validation rule.
"""

import logging

import pytest

from fastnet2n2k.__main__ import _QuietTransientCanErrors, fnv_unique, parse_args


# ── _QuietTransientCanErrors ──────────────────────────────────────────────────
# This filter matches library log messages by substring, so an upstream reword
# would silently stop it working and the journal would flood again. These tests are
# the tripwire for that: if the phrases drift, a test fails instead of the boat's
# journal. See the class docstring for what each case is.

def _record(msg, *args, level=logging.WARNING):
    return logging.LogRecord("x", level, __file__, 1, msg, args, None)


@pytest.fixture
def filt():
    return _QuietTransientCanErrors()


def test_drops_transmit_queue_full_spam(filt):
    assert filt.filter(_record("python-can transmit queue full, retrying send")) is False


def test_drops_send_failed_without_reconnecting(filt):
    assert filt.filter(_record("send failed without reconnecting")) is False


def test_passes_a_genuine_error_through(filt):
    assert filt.filter(_record("Connection lost while reading", level=logging.ERROR)) is True


def test_passes_ordinary_records_through(filt):
    assert filt.filter(_record("Address claimed: %d", 100)) is True


def test_drops_an_unrenderable_record(filt):
    """Case 2: a record whose args can't be formatted (the seed can.Message with a
    string timestamp). getMessage() raising is itself the signal to drop it."""
    class Unformattable:
        def __str__(self):
            raise ValueError("Unknown format code 'f' for object of type 'str'")

    assert filt.filter(_record("sending: %f", Unformattable())) is False


# ── fnv_unique ────────────────────────────────────────────────────────────────

def test_fnv_unique_is_stable_and_in_range():
    """Must be the same across calls (so the device keeps its NAME across reboots)
    and fit the 21-bit NMEA2000 field."""
    a, b = fnv_unique(), fnv_unique()
    assert a == b
    assert 0 <= a < (1 << 21)


# ── parse_args validation ─────────────────────────────────────────────────────

def test_priority_out_of_range_is_rejected(monkeypatch):
    monkeypatch.setattr("sys.argv", ["fastnet2n2k", "--serial", "/dev/x", "--n2k-priority", "9"])
    with pytest.raises(SystemExit):
        parse_args()


def test_priority_in_range_is_accepted(monkeypatch):
    monkeypatch.setattr("sys.argv", ["fastnet2n2k", "--serial", "/dev/x", "--n2k-priority", "3"])
    assert parse_args().n2k_priority == 3


def test_serial_and_file_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr("sys.argv", ["fastnet2n2k", "--serial", "/dev/x", "--file", "cap.txt"])
    with pytest.raises(SystemExit):
        parse_args()
