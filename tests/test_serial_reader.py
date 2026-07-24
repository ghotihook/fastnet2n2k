"""The SerialReader reopen watchdogs.

Two failure modes of the Pi UART on its first open after a cold boot must both lead
to a close/reopen (the automated form of the "stop it and run it again" workaround):

- the port goes silent — no bytes ever reach the fd;
- the port delivers bytes continuously that never decode into a Fastnet frame
  (a misconfigured line: wrong divisor or parity).

The second is the one a silence-only watchdog cannot see, so it is the point of these
tests. The poll loop's timing is driven by moving a fake clock rather than sleeping.
"""

import asyncio

import pytest

from fastnet2n2k import input_source
from fastnet2n2k.input_source import (
    NO_FRAME_REOPEN_INTERVAL,
    SILENCE_REOPEN_INTERVAL,
    SerialReader,
)


class FakePort:
    """Minimal stand-in for serial.Serial: hands out ``data`` on every read."""

    def __init__(self, data=b""):
        self.port = "/dev/fake"
        self.data = data
        self.opens = 0
        self._fd = 3
        self._served = False

    def fileno(self):
        return self._fd

    def read(self, _size):
        # One chunk per drain: _drain loops until a read comes back empty, so alternate
        # payload/empty. An empty `data` is a permanently silent port.
        self._served = not self._served
        return self.data if self._served else b""

    def close(self):
        pass

    def open(self):
        self.opens += 1
        self._served = False
        self._fd += 1   # a reopen yields a new fd, as the real port does


class FakeLoop:
    def __init__(self):
        self.readers = set()

    def add_reader(self, fd, _cb):
        self.readers.add(fd)

    def remove_reader(self, fd):
        self.readers.discard(fd)


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the tests advance by hand."""
    now = [1000.0]
    monkeypatch.setattr(input_source.time, "monotonic", lambda: now[0])
    return now


def make_reader(data=b""):
    port = FakePort(data)
    reader = SerialReader(FakeLoop(), port, asyncio.Queue())
    reader._attach()
    return reader, port


def tick(reader):
    """One poll-loop iteration, minus the sleep."""
    reader._drain()
    now = input_source.time.monotonic()
    if now - reader._last_rx >= SILENCE_REOPEN_INTERVAL:
        reader._reopen("no data")
    elif now - reader._last_frame >= NO_FRAME_REOPEN_INTERVAL:
        reader._reopen("no decodable frames")


def test_undecodable_stream_triggers_reopen(clock):
    """Bytes keep arriving but never decode — the silence watchdog stays quiet, so
    the no-frame watchdog must be what reopens the port. This is the first-boot bug."""
    reader, port = make_reader(b"\x00" * 64)

    clock[0] += NO_FRAME_REOPEN_INTERVAL - 1
    tick(reader)
    assert port.opens == 0, "reopened before the no-frame interval elapsed"

    clock[0] += 2
    tick(reader)
    assert port.opens == 1
    assert reader._reopens == 1


def test_decoding_stream_is_never_reopened(clock):
    """A port producing frames is left alone, however long it runs."""
    reader, port = make_reader(b"\x00" * 64)

    for _ in range(10):
        clock[0] += NO_FRAME_REOPEN_INTERVAL / 2
        reader.note_frame()
        tick(reader)

    assert port.opens == 0


def test_silence_triggers_reopen_first(clock):
    """No bytes at all trips the shorter, more specific silence path."""
    reader, port = make_reader()   # reads always return b""

    clock[0] += SILENCE_REOPEN_INTERVAL + 0.1
    tick(reader)

    assert port.opens == 1


def test_reopen_resets_both_clocks(clock):
    """After a silence reopen the no-frame clock must not be stale, or the port would
    be reopened again on the very next tick."""
    reader, port = make_reader()

    clock[0] += SILENCE_REOPEN_INTERVAL + 0.1
    tick(reader)
    assert port.opens == 1

    clock[0] += 0.1
    tick(reader)
    assert port.opens == 1, "second reopen fired immediately — clocks not both reset"


def test_reopen_reattaches_the_new_fd(clock):
    """The fd changes across a reopen; the old one must be dropped from the loop."""
    reader, port = make_reader()
    old_fd = port.fileno()

    clock[0] += SILENCE_REOPEN_INTERVAL + 0.1
    tick(reader)

    assert reader._loop.readers == {port.fileno()}
    assert old_fd not in reader._loop.readers


def test_note_frame_reports_recovery(clock, caplog):
    """Recovery is reported on a decoded frame, not on bytes alone — bytes are what
    the broken port was producing in the first place."""
    reader, port = make_reader()

    clock[0] += SILENCE_REOPEN_INTERVAL + 0.1
    tick(reader)
    assert reader._reopens == 1

    with caplog.at_level("INFO", logger="fastnet2n2k.input"):
        reader.note_frame()
    assert "decoding again after 1 reopen" in caplog.text
    assert reader._reopens == 0
