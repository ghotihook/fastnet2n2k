"""Unit tests for the Fastnet → NMEA2000 mapping.

Feeds pyfastnet's bundled capture files through the decoder into the live store,
then exercises the mapping triggers and round-trips the resulting NMEA2000 messages
with the n2k parsers to confirm PGN, units, T/M reference and sign.
"""

import math
import os

import pytest
from fastnet_decoder import FrameBuffer
from n2k import types as t
from n2k.messages import (
    parse_n2k_attitude,
    parse_n2k_boat_speed,
    parse_n2k_cog_sog_rapid,
    parse_n2k_heading,
    parse_n2k_temperature,
    parse_n2k_water_depth,
    parse_n2k_wind_speed,
)

from fastnet2n2k import mapping
from fastnet2n2k.live_store import live_data, update_live_data

CAPTURES = "/Users/alex060/Prod/pyfastnet/temp"
KN_MS = 0.514444


def load_capture(name):
    """Reset the live store and replay a capture file into it."""
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


@pytest.fixture(autouse=True)
def _example1():
    load_capture("example1_fastnet_data.txt")


def test_heading_magnetic():
    msg = mapping.process_heading()
    assert msg.pgn == 127250
    h = parse_n2k_heading(msg)
    assert h.ref == t.N2kHeadingReference.magnetic          # from "°M" layout
    assert math.degrees(h.heading) == pytest.approx(319, abs=0.05)


def test_apparent_wind_units_and_reference():
    msg = mapping.process_apparent_wind()
    assert msg.pgn == 130306
    w = parse_n2k_wind_speed(msg)
    assert w.wind_reference == t.N2kWindReference.Apparent
    assert math.degrees(w.wind_angle) == pytest.approx(85, abs=0.05)
    assert w.wind_speed == pytest.approx(16.3 * KN_MS, abs=0.01)   # knots → m/s


def test_true_wind_direction_reference_from_layout():
    msg = mapping.process_twd()
    assert msg.pgn == 130306
    w = parse_n2k_wind_speed(msg)
    assert w.wind_reference == t.N2kWindReference.Magnetic          # "°M"
    assert math.degrees(w.wind_angle) == pytest.approx(46, abs=0.05)


def test_boatspeed_knots_to_ms():
    w = parse_n2k_boat_speed(mapping.process_boatspeed())
    assert w.water_referenced == pytest.approx(0.87 * KN_MS, abs=0.01)


def test_depth_metres_passthrough():
    d = parse_n2k_water_depth(mapping.process_depth())
    assert d.depth_below_transducer == pytest.approx(15.2, abs=0.01)


def test_cog_sog_prefers_true():
    msg = mapping.process_cog_sog()
    c = parse_n2k_cog_sog_rapid(msg)
    assert c.heading_reference == t.N2kHeadingReference.true        # COG (True) present
    assert math.degrees(c.cog) == pytest.approx(336, abs=0.05)
    assert c.sog == pytest.approx(1.7 * KN_MS, abs=0.01)


def test_set_drift_manual_pgn():
    msg = mapping.process_set_drift()
    assert msg.pgn == 129291
    assert len(msg.data) == 8
    # Decode the hand-built frame: SID | ref(2b) | set(0.0001rad) | drift(0.01 m/s) | rsv
    ref = msg.data[1] & 0x03
    set_raw   = int.from_bytes(msg.data[2:4], "little")
    drift_raw = int.from_bytes(msg.data[4:6], "little")
    assert ref == int(t.N2kHeadingReference.magnetic)            # "°M" layout
    assert math.degrees(set_raw * 0.0001) == pytest.approx(277, abs=0.05)
    assert drift_raw * 0.01 == pytest.approx(0.78 * KN_MS, abs=0.01)


def test_sea_temp_c_to_kelvin():
    temp = parse_n2k_temperature(mapping.process_sea_temp())
    assert temp.temp_source == t.N2kTempSource.SeaTemperature
    assert temp.actual_temperature == pytest.approx(24 + 273.15, abs=0.05)


def test_unavailable_channel_sends_nothing():
    # Heel/Trim are " OFF" (value None) in this capture → no attitude frame.
    assert mapping.process_attitude() is None
    assert mapping.process_pressure() is None


def test_heel_sign_passthrough():
    # pyfastnet already applies the sign; mapping must pass it through unchanged.
    load_capture("heel-port.txt")
    port = parse_n2k_attitude(mapping.process_attitude())
    load_capture("heel-stb.txt")
    stb = parse_n2k_attitude(mapping.process_attitude())
    assert port.roll < 0 < stb.roll
    assert math.degrees(port.roll) == pytest.approx(-19.6, abs=0.1)
    assert math.degrees(stb.roll) == pytest.approx(33.7, abs=0.1)


class _StubNode:
    def __init__(self):
        self.sent = []

    def send_msg(self, msg):
        self.sent.append(msg)
        return True


def test_throttle_min_interval():
    node = _StubNode()
    mapping.set_node(node)
    mapping._channel_last_sent.clear()
    update_live_data("Depth (Meters)", "0xC1", 15.2, "15.2", None)
    mapping.process_channel("Depth (Meters)", None)          # first → sends
    mapping.process_channel("Depth (Meters)", None)          # <0.05s later → throttled
    assert len(node.sent) == 1
    assert node.sent[0].pgn == 128267


def test_no_send_when_trigger_returns_none():
    node = _StubNode()
    mapping.set_node(node)
    mapping._channel_last_sent.clear()
    update_live_data("Heel Angle", "0x34", None, " OFF", None)
    mapping.process_channel("Heel Angle", None)
    assert node.sent == []
