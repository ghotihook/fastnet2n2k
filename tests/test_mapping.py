"""Unit tests for the Fastnet (Signal K) → NMEA2000 mapping (tomer-w/nmea2000 backend).

Feeds pyfastnet v3's bundled captures through the decoder (which now emits
{signalk_path: SI_value}) into the live store, then exercises the mapping triggers.
Each trigger returns an ``NMEA2000Message``; we check the field values (SI units), the
T/M reference strings and the sign, and confirm every produced message actually
encodes through the canboat codec.
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
    mapping.set_ignored_pgns(())   # module state: don't leak suppression between tests
    with open(os.path.join(CAPTURES, name)) as f:
        data = bytes.fromhex(f.read().strip().replace(" ", "").replace("\n", ""))
    fb = FrameBuffer()   # v3: queues {signalk_path: SI_value}
    for i in range(0, len(data), 256):
        fb.add_to_buffer(data[i:i + 256])
        fb.get_complete_frames()
        while not fb.frame_queue.empty():
            for path, value in fb.frame_queue.get().get("values", {}).items():
                update_live_data(path, value)


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
    assert fval(msg, "reference") == "Magnetic"            # from headingMagnetic path
    assert math.degrees(fval(msg, "heading")) == pytest.approx(319, abs=0.05)
    assert encodes(msg)


def test_apparent_wind_units_and_reference():
    msg = mapping.process_apparent_wind()
    assert msg.PGN == 130306
    assert fval(msg, "reference") == "Apparent"
    assert math.degrees(fval(msg, "windAngle")) == pytest.approx(85, abs=0.05)
    assert fval(msg, "windSpeed") == pytest.approx(8.30, abs=0.01)   # native m/s
    assert encodes(msg)


def test_true_wind_direction_reference_from_path():
    msg = mapping.process_twd()
    assert fval(msg, "reference") == "Magnetic (ground referenced to Magnetic North)"
    assert math.degrees(fval(msg, "windAngle")) == pytest.approx(46, abs=0.05)
    assert encodes(msg)


def test_boatspeed_ms_passthrough():
    msg = mapping.process_boatspeed()
    assert fval(msg, "speedWaterReferenced") == pytest.approx(0.87 * KN_MS, abs=0.01)
    assert encodes(msg)


def test_depth_metres_passthrough():
    msg = mapping.process_depth()
    assert fval(msg, "depth") == pytest.approx(15.2, abs=0.01)
    assert fval(msg, "offset") is None   # offset "not available": Fastnet doesn't report it
    assert encodes(msg)


def test_cog_sog_prefers_true():
    msg = mapping.process_cog_sog()
    assert fval(msg, "cogReference") == "True"
    assert math.degrees(fval(msg, "cog")) == pytest.approx(336, abs=0.05)
    assert fval(msg, "sog") == pytest.approx(1.7 * KN_MS, abs=0.01)
    assert encodes(msg)


def test_sea_temp_kelvin_passthrough():
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
    assert mapping.process_attitude() is None    # heel/trim are display-only in example1
    assert mapping.process_pressure() is None


def test_position_emitted():
    msg = mapping.process_position()
    assert msg.PGN == 129025
    assert fval(msg, "latitude") == pytest.approx(-33.8607, abs=1e-3)
    assert fval(msg, "longitude") == pytest.approx(151.236, abs=1e-3)
    assert encodes(msg)


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
    update_live_data("environment.depth.belowTransducer", 15.2)
    asyncio.run(mapping.process_channel("environment.depth.belowTransducer"))   # first → sends
    asyncio.run(mapping.process_channel("environment.depth.belowTransducer"))   # <0.05s → throttled
    assert len(dev.sent) == 1
    assert dev.sent[0].PGN == 128267


def test_no_send_when_trigger_returns_none():
    dev = _StubDevice()
    mapping.set_device(dev)
    mapping._channel_last_sent.clear()
    update_live_data("navigation.attitude.roll", None)
    asyncio.run(mapping.process_channel("navigation.attitude.roll"))
    assert dev.sent == []


def test_build_error_is_isolated(monkeypatch):
    # A handler that raises must not crash the bridge or send anything.
    @mapping.emits(128267)
    def boom():
        raise ValueError("bad value")
    monkeypatch.setitem(mapping._CHANNEL_MAP, "environment.depth.belowTransducer", boom)
    dev = _StubDevice()
    mapping.set_device(dev)
    mapping._channel_last_sent.clear()
    update_live_data("environment.depth.belowTransducer", 15.2)
    asyncio.run(mapping.process_channel("environment.depth.belowTransducer"))   # must not raise
    assert dev.sent == []


# ── --ignore-pgn suppression ──────────────────────────────────────────────────

def test_suppressed_pgn_is_not_sent():
    dev = _StubDevice()
    mapping.set_device(dev)
    mapping.set_ignored_pgns({128267})           # depth
    update_live_data("environment.depth.belowTransducer", 15.2)
    update_live_data("navigation.speedThroughWater", 3.0)
    asyncio.run(mapping.process_channel("environment.depth.belowTransducer"))
    asyncio.run(mapping.process_channel("navigation.speedThroughWater"))
    assert [msg.PGN for msg in dev.sent] == [128259]   # boatspeed unaffected


def test_suppressing_a_shared_pgn_silences_every_path_on_it():
    """130306 carries apparent wind, true wind and TWD — suppressing it drops all
    three. Documented behaviour, not a bug: --ignore-pgn works at PGN granularity."""
    dev = _StubDevice()
    mapping.set_device(dev)
    mapping.set_ignored_pgns({130306})
    for path in ("environment.wind.angleApparent", "environment.wind.angleTrueWater",
                 "environment.wind.directionMagnetic"):
        asyncio.run(mapping.process_channel(path))
    assert dev.sent == []


def test_suppressed_pgns_are_not_advertised():
    mapping.set_ignored_pgns({130306, 128267})
    advertised = mapping.tx_pgns()
    assert 130306 not in advertised and 128267 not in advertised
    assert set(advertised) == set(mapping.TX_PGNS) - {130306, 128267}


def test_every_trigger_declares_its_pgn():
    """The @emits tag is what suppression and TX_PGNS both read; an untagged trigger
    would raise on the send path instead of being filterable."""
    for path, trigger in mapping._CHANNEL_MAP.items():
        assert isinstance(getattr(trigger, "pgn", None), int), f"{path} has no @emits"


def test_tx_pgns_matches_what_the_triggers_actually_build():
    """TX_PGNS is derived from the tags, so this checks the tags themselves are
    honest: what each trigger builds must be what it claims to emit."""
    for path, trigger in mapping._CHANNEL_MAP.items():
        msg = mapping.trigger_n2k_frame(path)
        if msg is not None:
            assert msg.PGN == trigger.pgn, f"{path} tagged {trigger.pgn}, built {msg.PGN}"


class _NotReadyDevice(_StubDevice):
    ready = False


def _process_depth_once():
    update_live_data("environment.depth.belowTransducer", 15.2)
    asyncio.run(mapping.process_channel("environment.depth.belowTransducer"))


def test_not_ready_is_tolerated_then_exits(monkeypatch):
    """F2: a not-ready device is tolerated (dropped, not raised) up to
    NOT_READY_EXIT_AFTER, then raises DeviceNotReadyError so run() can restart us."""
    mapping.set_device(_NotReadyDevice())
    mapping._channel_last_sent.clear()
    mapping._not_ready_since = None
    clock = [1000.0]
    monkeypatch.setattr(mapping.time, "monotonic", lambda: clock[0])

    _process_depth_once()                       # first not-ready: starts the clock
    clock[0] += mapping.NOT_READY_EXIT_AFTER - 1
    _process_depth_once()                       # still within grace: tolerated

    clock[0] += 2                               # now past the threshold
    with pytest.raises(mapping.DeviceNotReadyError):
        _process_depth_once()


def test_not_ready_clock_resets_when_the_device_recovers(monkeypatch):
    """A brief not-ready blip must not count toward the exit once the device is back."""
    dev = _NotReadyDevice()
    mapping.set_device(dev)
    mapping._channel_last_sent.clear()
    mapping._not_ready_since = None
    clock = [1000.0]
    monkeypatch.setattr(mapping.time, "monotonic", lambda: clock[0])

    _process_depth_once()                       # not-ready: clock starts
    dev.ready = True
    clock[0] += 1
    _process_depth_once()                       # recovered → clock reset
    assert mapping._not_ready_since is None

    dev.ready = False
    clock[0] += mapping.NOT_READY_EXIT_AFTER - 1
    _process_depth_once()                       # fresh grace window, no raise
