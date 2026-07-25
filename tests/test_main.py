"""Entry-point pieces that are pure enough to test cheaply.

Not the run() loop — that needs a live CAN device and event loop, and mocking it
into a permanent test would be brittle for little gain. These are the standalone
bits: the log filter (fragile, and load-bearing when the bus misbehaves), the NAME
hash (a stability contract), and the one argument that has a validation rule.
"""

import asyncio
import logging
import types

import pytest

from fastnet2n2k import __main__ as main_mod
from fastnet2n2k.__main__ import _QuietTransientCanErrors, fnv_unique, parse_args
from fastnet2n2k.input_source import SERIAL_CLOSED


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


# ── --ignore-pgn parsing ──────────────────────────────────────────────────────
# Repeatable and comma-separated both have to work: a systemd unit line reads better
# comma-separated, an interactive invocation often repeats the flag.

def _args(*extra, monkeypatch):
    monkeypatch.setattr("sys.argv", ["fastnet2n2k", "--serial", "/dev/x", *extra])
    return parse_args()


def test_ignore_pgn_defaults_to_nothing_suppressed(monkeypatch):
    assert _args(monkeypatch=monkeypatch).ignore_pgn == frozenset()


def test_ignore_pgn_accepts_repeated_and_comma_separated(monkeypatch):
    args = _args("--ignore-pgn", "130312,128275", "--ignore-pgn", "127251",
                 monkeypatch=monkeypatch)
    assert args.ignore_pgn == {130312, 128275, 127251}


def test_ignore_pgn_rejects_a_pgn_we_do_not_transmit(monkeypatch):
    """A typo must fail loudly — silently ignoring it would look like it worked
    while the frames kept flowing."""
    with pytest.raises(SystemExit):
        _args("--ignore-pgn", "130316", monkeypatch=monkeypatch)


def test_ignore_pgn_rejects_non_numeric(monkeypatch):
    with pytest.raises(SystemExit):
        _args("--ignore-pgn", "depth", monkeypatch=monkeypatch)


# ── run() fault tolerance (F1) ────────────────────────────────────────────────
# The headline fix: a dead serial port must make run() EXIT so systemd restarts it,
# never leave it parked on queue.get() forever. A regression here is a silent hang
# that no test would otherwise catch — so this drives the real run() wiring, and the
# wait_for timeout turns a hang into a failure instead of a stuck test run.

class _FakeDevice:
    address = 100
    ready = True

    async def start(self): pass
    async def wait_ready(self): pass
    async def close(self): pass


def test_run_exits_when_the_serial_port_dies(monkeypatch):
    class _DyingReader:
        def __init__(self, _loop, _ser, queue):
            self._queue = queue

        def start(self):
            self._queue.put_nowait(SERIAL_CLOSED)   # port died before any data

        def stop(self): pass

    monkeypatch.setattr(main_mod, "make_device", lambda args: _FakeDevice())
    monkeypatch.setattr(main_mod, "open_serial_port", lambda dev: object())
    monkeypatch.setattr(main_mod, "SerialReader", _DyingReader)

    args = types.SimpleNamespace(channel="can0", n2k_priority=None, serial="/dev/x",
                                 file=None, live_data=False, unique=1,
                                 ignore_pgn=frozenset())
    logging.disable(logging.CRITICAL)
    try:
        rc = asyncio.run(asyncio.wait_for(main_mod.run(args), timeout=2))
    finally:
        logging.disable(logging.NOTSET)
    assert rc == 1   # exited (and, thanks to wait_for, provably did not hang)
