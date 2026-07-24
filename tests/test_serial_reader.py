"""SerialReader: draining a live port into the decode queue.

The reader is deliberately thin — add_reader wakes the loop, _drain empties the port
into the queue. What's worth pinning down is that it drains to empty in one wakeup
(a burst larger than READ_SIZE must not be left behind) and that a broken fd stops
the reader instead of spinning on it.
"""

import asyncio

from fastnet2n2k.input_source import READ_SIZE, SerialReader


class FakePort:
    """Hands out `chunks` one per read(), then empty. `fail` raises instead."""

    def __init__(self, chunks=(), fail=None):
        self.port = "/dev/fake"
        self.chunks = list(chunks)
        self.fail = fail
        self.closed = False

    def fileno(self):
        return 3

    def read(self, _size):
        if self.fail:
            raise self.fail
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class FakeLoop:
    def __init__(self):
        self.readers = set()

    def add_reader(self, fd, _cb):
        self.readers.add(fd)

    def remove_reader(self, fd):
        self.readers.discard(fd)


def make_reader(port):
    reader = SerialReader(FakeLoop(), port, asyncio.Queue())
    reader.start()
    return reader


def test_drain_empties_the_port_in_one_wakeup():
    """A burst bigger than READ_SIZE must be taken whole, not one chunk per wakeup —
    the fast path fires once per readable notification, not once per buffered byte."""
    port = FakePort([b"a" * READ_SIZE, b"b" * READ_SIZE, b"c"])
    reader = make_reader(port)

    reader._drain()

    assert reader._queue.qsize() == 3


def test_start_registers_and_stop_unregisters_the_fd():
    port = FakePort()
    reader = make_reader(port)
    assert reader._loop.readers == {3}

    reader.stop()

    assert reader._loop.readers == set()
    assert port.closed


def test_stop_is_safe_to_call_twice():
    """run()'s finally block calls stop() even when _drain already gave up."""
    reader = make_reader(FakePort())
    reader.stop()
    reader.stop()


def test_read_failure_stops_the_reader_rather_than_spinning():
    """add_reader would call _drain in a tight loop on a broken fd."""
    port = FakePort(fail=OSError("device went away"))
    reader = make_reader(port)

    reader._drain()

    assert reader._loop.readers == set()
    assert port.closed
