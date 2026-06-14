"""End-to-end smoke test of the full pipeline with the CAN bus / node stubbed out.

Runs ``__main__.main()`` against a capture file and asserts that the expected NMEA2000
PGNs were handed to the node for transmission.
"""

import sys

import pytest

from fastnet2n2k import __main__ as cli
from fastnet2n2k import input_source, mapping

CAPTURE = "/Users/alex060/Prod/pyfastnet/temp/example1_fastnet_data.txt"


class _StubBus:
    def shutdown(self):
        pass


class _StubNotifier:
    def __init__(self, *a, **k):
        pass

    def add_listener(self, *a):
        pass

    def stop(self):
        pass


class _StubNode:
    def __init__(self, *a, **k):
        self.sent = []
        self.n2k_source = 0
        self.transmit_messages = []

    def set_product_information(self, *a, **k):
        pass

    def set_configuration_information(self, *a, **k):
        pass

    def send_msg(self, msg):
        self.sent.append(msg)
        return True


def test_full_pipeline_emits_expected_pgns(monkeypatch):
    node = _StubNode()
    monkeypatch.setattr(cli.can, "Bus", lambda *a, **k: _StubBus())
    monkeypatch.setattr(cli.can, "Notifier", _StubNotifier)
    monkeypatch.setattr(cli.n2k, "Node", lambda *a, **k: node)
    monkeypatch.setattr(input_source, "FILE_READ_DELAY", 0)
    mapping._channel_last_sent.clear()
    monkeypatch.setattr(sys, "argv", ["fastnet2n2k", "--file", CAPTURE, "--channel", "can0"])

    assert cli.main() == 0

    pgns = {m.pgn for m in node.sent}
    # Core sailing instruments present in example1 should all have been emitted.
    for expected in {127250, 130306, 128259, 128267, 129026, 130312, 127245, 129025}:
        assert expected in pgns, f"missing PGN {expected}; got {sorted(pgns)}"
    assert node.transmit_messages, "node should advertise its transmit PGN list"
