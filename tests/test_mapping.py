"""Unit tests for the Fastnet → NMEA2000 mapping (tomer-w/nmea2000 backend).

Feeds pyfastnet's bundled captures through the decoder into the live store, then
exercises the mapping triggers. Each trigger returns an ``NMEA2000Message``; we check
the field values (SI units), the T/M reference strings and the sign, and confirm every
produced message actually encodes through the canboat codec.
"""

import asyncio
import math
import os

import pytest
from fastnet_decoder import FrameBuffer
from nmea2000.encoder import NMEA2000Encoder
from nmea2000.input_formats import N2KFormat
import nmea2000.encoder_formats  # noqa: F401  (registers formats)

from fastnet2n2k import mapping
from fastnet2n2k.live_store import live_data, update_live_data

CAPTURES = os.path.join(os.path.dirname(__file__), "data")
KN_MS = 0.514444
_ENC = NMEA2000Encoder(N2KFormat.CAN_FRAME_ASCII)


def load_capture(name):
    live_data.clear()
    mapping._channel_last_sent.clear()
    with open(os.path.join(CAPTURES, name)) as f:
        data = bytes.fromhex(f.read().strip().replace(" ", "").replace("\n", ""))
    fb = FrameBuffer()
    for i in range(0, len(data), 256):
        fb.add_to_buffer(data[i:i + 256])
        fb.get_complete_frames()
        while not fb.frame_queue.empty():
            for ch, d in fb.frame_queue.get().get("values", {}).items():
                update_live_data(ch, d.get("channel_id"), d.get("value"),
                                 d.get("display_text"), d.get("layout"))


def fval(msg, fid):
    return next(f.value for f in msg.fields if f.id == fid)


def encodes(msg):
    """The message round-trips through the canboat encoder without error."""
    return bool(_ENC.encode(msg))


@pytest.fixture(autouse=True)
def _example1():
    load_capture("example1_fastnet_data.txt")


def test_heading_magnetic():
    msg = mapping.process_heading()
    assert msg.PGN == 127250
    assert fval(msg, "reference") == "Magnetic"            # from "°M" layout
    assert math.degrees(fval(msg, "heading")) == pytest.approx(319, abs=0.05)
    assert encodes(msg)


def test_apparent_wind_units_and_reference():
    msg = mapping.process_apparent_wind()
    assert msg.PGN == 130306
    assert fval(msg, "reference") == "Apparent"
    assert math.degrees(fval(msg, "windAngle")) == pytest.approx(85, abs=0.05)
    assert fval(msg, "windSpeed") == pytest.approx(16.3 * KN_MS, abs=0.01)
    assert encodes(msg)


def test_true_wind_direction_reference_from_layout():
    msg = mapping.process_twd()
    assert fval(msg, "reference") == "Magnetic (ground referenced to Magnetic North)"
    assert math.degrees(fval(msg, "windAngle")) == pytest.approx(46, abs=0.05)
    assert encodes(msg)


def test_boatspeed_knots_to_ms():
    msg = mapping.process_boatspeed()
    assert fval(msg, "speedWaterReferenced") == pytest.approx(0.87 * KN_MS, abs=0.01)
    assert encodes(msg)


def test_depth_metres_passthrough():
    msg = mapping.process_depth()
    assert fval(msg, "depth") == pytest.approx(15.2, abs=0.01)
    assert encodes(msg)


def test_cog_sog_prefers_true():
    msg = mapping.process_cog_sog()
    assert fval(msg, "cogReference") == "True"
    assert math.degrees(fval(msg, "cog")) == pytest.approx(336, abs=0.05)
    assert fval(msg, "sog") == pytest.approx(1.7 * KN_MS, abs=0.01)
    assert encodes(msg)


def test_sea_temp_c_to_kelvin():
    msg = mapping.process_sea_temp()
    assert fval(msg, "source") == "Sea Temperature"
    assert fval(msg, "actualTemperature") == pytest.approx(24 + 273.15, abs=0.05)
    assert encodes(msg)


def test_set_drift_native_pgn():
    msg = mapping.process_set_drift()
    assert msg.PGN == 129291
    assert fval(msg, "setReference") == "Magnetic"
    assert math.degrees(fval(msg, "set")) == pytest.approx(277, abs=0.05)
    assert fval(msg, "drift") == pytest.approx(0.78 * KN_MS, abs=0.01)
    assert encodes(msg)


def test_unavailable_channel_sends_nothing():
    assert mapping.process_attitude() is None    # heel/trim are " OFF" in example1
    assert mapping.process_pressure() is None


def test_heel_sign_passthrough():
    load_capture("heel-port.txt")
    port = mapping.process_attitude()
    load_capture("heel-stb.txt")
    stb = mapping.process_attitude()
    assert fval(port, "roll") < 0 < fval(stb, "roll")
    assert math.degrees(fval(port, "roll")) == pytest.approx(-19.6, abs=0.1)
    assert math.degrees(fval(stb, "roll")) == pytest.approx(33.7, abs=0.1)


class _StubDevice:
    ready = True

    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


def test_throttle_min_interval():
    dev = _StubDevice()
    mapping.set_device(dev)
    mapping._channel_last_sent.clear()
    update_live_data("Depth (Meters)", "0xC1", 15.2, "15.2", None)
    asyncio.run(mapping.process_channel("Depth (Meters)"))   # first → sends
    asyncio.run(mapping.process_channel("Depth (Meters)"))   # <0.05s → throttled
    assert len(dev.sent) == 1
    assert dev.sent[0].PGN == 128267


def test_no_send_when_trigger_returns_none():
    dev = _StubDevice()
    mapping.set_device(dev)
    mapping._channel_last_sent.clear()
    update_live_data("Heel Angle", "0x34", None, " OFF", None)
    asyncio.run(mapping.process_channel("Heel Angle"))
    assert dev.sent == []


def test_build_error_is_isolated(monkeypatch):
    # A handler that raises must not crash the bridge or send anything.
    def boom():
        raise ValueError("bad value")
    monkeypatch.setitem(mapping._CHANNEL_MAP, "Depth (Meters)", boom)
    dev = _StubDevice()
    mapping.set_device(dev)
    mapping._channel_last_sent.clear()
    update_live_data("Depth (Meters)", "0xC1", 15.2, "15.2", None)
    asyncio.run(mapping.process_channel("Depth (Meters)"))   # must not raise
    assert dev.sent == []
