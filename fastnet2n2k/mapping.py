"""Map decoded Fastnet data (pyfastnet v3 Signal K paths) to NMEA2000 messages and
send them on the CAN bus.

Uses the ``nmea2000`` library (tomer-w), which is canboat-based: messages are built
by taking a blank PGN template from :mod:`nmea2000.pgns`, setting the fields we care
about (by id, in SI units), and handing the resulting ``NMEA2000Message`` to an
``N2KDevice`` that transmits it over SocketCAN and manages ISO address claiming.

Conventions:
- **Units**: pyfastnet v3 already emits SI (radians, m/s, Kelvin, Pascals, metres) —
  the same units NMEA2000 uses — so values pass straight through. No conversion here.
- **Sign** is taken directly from the decoded value.
- **T/M reference** is carried by the Signal K *path* (``navigation.headingMagnetic``
  vs ``headingTrue``, ``environment.wind.directionMagnetic`` vs ``directionTrue``,
  ``environment.current.setMagnetic`` vs ``setTrue``); the trigger reads whichever
  path is present. No layout field to inspect.
- **Cadence**: a PGN is sent on every update (event-driven) — a repeated value is
  still live data worth putting on the bus — debounced only by MIN_SEND_INTERVAL,
  which caps any one path's rate — except the B&G raw channels, which go out at full
  source rate for logging. When the source stops, output stops and consumers time the
  PGN out themselves.
"""

import logging
import time
from datetime import datetime, timezone
from math import tau

import nmea2000.pgns as pgns

from .live_store import get_live_data

logger = logging.getLogger("fastnet2n2k.mapping")

MIN_SEND_INTERVAL = 0.05    # per-path rate cap (~20 Hz); debounce only

# How long the device may stay not-ready *while we have frames to send* before we give
# up and exit. Normal address claim completes in a few seconds (wait_ready waits 10),
# so a value well past that means the nmea2000 background claim task has probably died
# — it is never retried if it does — and reconnecting from a fresh process is the only
# way back. Comfortably above the normal claim time so a healthy startup never trips it.
NOT_READY_EXIT_AFTER = 30.0

_channel_last_sent: dict = {}
_device = None
_priority_override = None
_ignored_pgns: frozenset = frozenset()   # --ignore-pgn: never built, sent or advertised
_not_ready_since = None      # monotonic time we first saw the device not-ready, or None

WARN_INTERVAL = 5.0         # per-key cap on the warnings below
_last_warned: dict = {}


class DeviceNotReadyError(RuntimeError):
    """The CAN device stayed not-ready for NOT_READY_EXIT_AFTER while frames were
    waiting to send. Raised so run() can exit cleanly and let systemd restart us,
    rather than reading Fastnet forever and transmitting nothing."""


def _warn_throttled(key, msg, *args):
    """Warn at most once per ``WARN_INTERVAL`` for ``key``. Both callers fire once
    per frame while the bus is unhappy, which would flood the journal.

    ``key`` just names the warning so each kind gets its own timer.
    """
    now = time.monotonic()
    last = _last_warned.get(key)
    if last is None or now - last >= WARN_INTERVAL:
        _last_warned[key] = now
        logger.warning(msg, *args)


def set_device(device) -> None:
    """Register the N2KDevice that frames are transmitted through."""
    global _device
    _device = device


def set_priority_override(priority) -> None:
    """Force every transmitted frame to ``priority`` (0–7), ignoring the per-PGN
    standard priorities. ``None`` restores the standard per-PGN behaviour."""
    global _priority_override
    _priority_override = priority


def set_ignored_pgns(pgns_) -> None:
    """Suppress these PGNs: paths whose trigger emits one are skipped before the
    message is ever built, and ``tx_pgns()`` drops them from what we advertise.
    An empty iterable restores normal behaviour (everything transmitted)."""
    global _ignored_pgns
    _ignored_pgns = frozenset(pgns_)


def emits(pgn, full_rate=False):
    """Tag a trigger with the PGN it builds.

    One declaration serves three readers: ``--ignore-pgn`` suppression (which needs
    to know a path's PGN *before* calling the trigger, so a suppressed path costs
    nothing), the advertised ``TX_PGNS`` list, and anyone reading the code. Keeping
    them derived from the same tag means they cannot drift apart.

    ``full_rate`` exempts the trigger from MIN_SEND_INTERVAL: every update is sent, at
    whatever rate the source produces it.
    """
    def tag(fn):
        fn.pgn = pgn
        fn.full_rate = full_rate
        return fn
    return tag


def _build(pgn, priority, variant=None, **fields):
    """Build an NMEA2000Message for ``pgn`` with the given field id → SI value pairs.

    ``None`` values are written through as "data not available". Source is left 0 so
    the N2KDevice substitutes its claimed address. ``variant`` picks one of several
    layouts sharing a PGN number — proprietary PGNs, where the manufacturer code in
    the payload decides — by the library's id for it, e.g. "bGKeyValueData".
    """
    # The nmea2000 library defines one function per PGN, named decode_pgn_<number>.
    # Called with an empty payload it hands back a blank message with all of that
    # PGN's fields present, which we then fill in — so this is "give me a blank
    # 130306 to fill in", looked up by number because there is no by-number API.
    # A shared number gets decode_pgn_<number>_<variant> per layout instead.
    name = f"decode_pgn_{pgn}_{variant}" if variant else f"decode_pgn_{pgn}"
    msg = getattr(pgns, name)(0, 0)
    msg.source = 0
    msg.priority = _priority_override if _priority_override is not None else priority
    msg.timestamp = datetime.now(timezone.utc)
    for f in msg.fields:
        if f.id in fields:
            f.raw_value = None
            f.value = fields[f.id]
    return msg


def _wrap(angle):
    """Normalise a radian angle into [0, 2π) for N2K angle fields."""
    if angle is None:
        return None
    return angle % tau


# ── Triggers: each returns one NMEA2000Message, or None ───────────────────────

def _wind(angle_path, speed_path, reference):
    angle = get_live_data(angle_path)   # radians
    speed = get_live_data(speed_path)   # m/s
    if angle is None and speed is None:
        return None
    return _build(130306, 2, windSpeed=speed, windAngle=_wrap(angle), reference=reference)


@emits(130306)
def process_apparent_wind():
    return _wind("environment.wind.angleApparent",
                 "environment.wind.speedApparent", "Apparent")


@emits(130306)
def process_true_wind():
    return _wind("environment.wind.angleTrueWater",
                 "environment.wind.speedTrue", "True (boat referenced)")


@emits(130306)
def process_twd():
    mag = get_live_data("environment.wind.directionMagnetic")
    tru = get_live_data("environment.wind.directionTrue")
    if mag is not None:
        direction, ref = mag, "Magnetic (ground referenced to Magnetic North)"
    elif tru is not None:
        direction, ref = tru, "True (ground referenced to North)"
    else:
        return None
    speed = get_live_data("environment.wind.speedTrue")
    return _build(130306, 2, windSpeed=speed, windAngle=_wrap(direction), reference=ref)


@emits(127250)
def process_heading():
    mag = get_live_data("navigation.headingMagnetic")
    tru = get_live_data("navigation.headingTrue")
    if mag is not None:
        heading, ref = mag, "Magnetic"
    elif tru is not None:
        heading, ref = tru, "True"
    else:
        return None
    return _build(127250, 2, heading=_wrap(heading), reference=ref,
                  deviation=None, variation=None)


@emits(128259)
def process_boatspeed():
    bs = get_live_data("navigation.speedThroughWater")   # m/s
    if bs is None:
        return None
    return _build(128259, 2, speedWaterReferenced=bs,
                  speedGroundReferenced=None, speedDirection=None)


@emits(128267)
def process_depth():
    # The H2000 applies its keel offset internally, so the number on the Fastnet
    # wire is already depth-below-keel (despite pyfastnet's belowTransducer label).
    # Fastnet never tells us the transducer-to-keel distance, so we send the PGN's
    # offset field as "not available" (None) — the honest encoding of "no offset
    # info", not offset=0 which would assert a transducer-at-keel distance we don't
    # know. Either way the depth value passes through unchanged; a consumer that
    # wants a below-keel path should apply its own offset (e.g. Signal K
    # transducerToKeel=0 + derived-data). See README "Depth is below keel".
    dm = get_live_data("environment.depth.belowTransducer")   # m, already below-keel
    if dm is None:
        return None
    return _build(128267, 3, depth=dm, offset=None, range=None)


@emits(127245)
def process_rudder():
    ra = get_live_data("steering.rudderAngle")   # rad
    if ra is None:
        return None
    return _build(127245, 2, instance=0, position=ra, angleOrder=None)


# pyfastnet's steering.autopilot.state → 127237 steeringMode. The standard has no wind
# mode, so wind steers as Heading Control, the same as compass: the pilot is holding a
# heading either way, and consumers can't tell the two apart. B&G "Power" is steering
# from the +/- buttons, i.e. non-follow-up.
_STEERING_MODE = {
    "standby":       "Main Steering",
    "auto":          "Heading Control",
    "wind":          "Heading Control",
    "directControl": "Non-Follow-Up Device",
    "route":         "Track Control",
}

# The 127237 fields Fastnet doesn't carry, sent as not-available rather than left at
# the blank template's zeros — which would claim e.g. "rudder limit not exceeded" and
# a 0° track. Lookup fields can't take None, so they get their all-ones raw value.
_AUTOPILOT_UNKNOWN = {
    "rudderLimitExceeded": 3, "offHeadingLimitExceeded": 3,     # 2-bit lookups
    "offTrackLimitExceeded": 3, "override": 3,
    "turnMode": 7, "commandedRudderDirection": 7,               # 3-bit lookups
    **dict.fromkeys(("commandedRudderAngle", "track", "rudderLimit", "offHeadingLimit",
                     "radiusOfTurnOrder", "rateOfTurnOrder", "offTrackLimit")),
}


@emits(127237)
def process_autopilot():
    mode = _STEERING_MODE.get(get_live_data("steering.autopilot.state"))
    if mode is None:
        return None
    # The compass target outlives the engagement — it stays on the wire in standby
    # while the boat turns away from it — so only send it while the pilot is actually
    # steering to a heading. Engaging reads 0° for one update before the real target
    # arrives; that passes through, since a 0° target is indistinguishable from north.
    target = None
    if mode in ("Heading Control", "Track Control"):
        target = _wrap(get_live_data("steering.autopilot.target.headingMagnetic"))
    return _build(127237, 2, steeringMode=mode, headingReference="Magnetic",
                  headingToSteerCourse=target,
                  vesselHeading=_wrap(get_live_data("navigation.headingMagnetic")),
                  **_AUTOPILOT_UNKNOWN)


@emits(128000)
def process_leeway():
    lw = get_live_data("navigation.leewayAngle")   # rad
    if lw is None:
        return None
    return _build(128000, 4, leewayAngle=lw)


@emits(129026)
def process_cog_sog():
    cog_true = get_live_data("navigation.courseOverGroundTrue")
    cog_mag = get_live_data("navigation.courseOverGroundMagnetic")
    sog = get_live_data("navigation.speedOverGround")   # m/s
    if sog is None:
        return None
    if cog_true is not None:
        return _build(129026, 2, cogReference="True", cog=_wrap(cog_true), sog=sog)
    if cog_mag is not None:
        return _build(129026, 2, cogReference="Magnetic", cog=_wrap(cog_mag), sog=sog)
    return _build(129026, 2, cog=None, sog=sog)


@emits(127508)
def process_battery():
    v = get_live_data("electrical.batteries.house.voltage")
    if v is None:
        return None
    return _build(127508, 6, instance=0, voltage=v, current=None, temperature=None)


@emits(127257)
def process_attitude():
    roll = get_live_data("navigation.attitude.roll")     # rad
    pitch = get_live_data("navigation.attitude.pitch")   # rad
    if roll is None and pitch is None:
        return None
    return _build(127257, 3, yaw=None, pitch=pitch, roll=roll)


@emits(130314)
def process_pressure():
    bp = get_live_data("environment.outside.pressure")   # Pa
    if bp is None:
        return None
    return _build(130314, 5, instance=0, source="Atmospheric", pressure=bp)


@emits(130312)
def process_sea_temp():
    k = get_live_data("environment.water.temperature")   # Kelvin
    if k is None:
        return None
    return _build(130312, 5, instance=0, source="Sea Temperature",
                  actualTemperature=k, setTemperature=None)


@emits(130312)
def process_air_temp():
    k = get_live_data("environment.outside.temperature")   # Kelvin
    if k is None:
        return None
    return _build(130312, 5, instance=0, source="Outside Temperature",
                  actualTemperature=k, setTemperature=None)


@emits(128275)
def process_distance_log():
    stored = get_live_data("navigation.log")        # m
    trip = get_live_data("navigation.trip.log")     # m
    if stored is None and trip is None:
        return None
    now = datetime.now(timezone.utc)
    return _build(128275, 6, date=now.date(), time=now.time(),
                  log=int(stored) if stored is not None else None,
                  tripLog=int(trip) if trip is not None else None)


@emits(129283)
def process_xte():
    xte = get_live_data("navigation.courseGreatCircle.crossTrackError")   # m
    if xte is None:
        return None
    return _build(129283, 3, xteMode="Autonomous", navigationTerminated="No", xte=xte)


@emits(127251)
def process_rate_of_turn():
    yr = get_live_data("navigation.rateOfTurn")   # rad/s
    if yr is None:
        return None
    return _build(127251, 2, rate=yr)


@emits(129291)
def process_set_drift():
    set_mag = get_live_data("environment.current.setMagnetic")
    set_tru = get_live_data("environment.current.setTrue")
    if set_mag is not None:
        set_val, ref = set_mag, "Magnetic"
    elif set_tru is not None:
        set_val, ref = set_tru, "True"
    else:
        return None
    drift = get_live_data("environment.current.drift")   # m/s
    return _build(129291, 3, setReference=ref, set=_wrap(set_val),
                  drift=max(0.0, drift) if drift is not None else None)


@emits(129025)
def process_position():
    pos = get_live_data("navigation.position")   # {"latitude", "longitude"} degrees
    if not pos:
        return None
    return _build(129025, 2, latitude=pos["latitude"], longitude=pos["longitude"])


# ── B&G raw sensor channels (130824 key-value) ────────────────────────────────
# The uncalibrated sensor readings, for logging. There is no standard PGN for them,
# so they go out in B&G's own proprietary key-value PGN, 130824, in B&G's layout:
# after the manufacturer header, a 12-bit key and 4-bit byte length, then the value.
# B&G's keys are Fastnet channel numbers (canboat, BANDG_KEY_VALUE: 65 Water Speed =
# 0x41, 127 VMG = 0x7F, ...), so each raw channel keeps its own channel number here.
# B&G gear has never been seen sending these four keys; the value format is ours:
# Fastnet carries them as signed 16-bit (format 0x0A), and they pass through as that,
# unscaled.
#
# Sent at full rate, one key per message: header + one key/value is 6 bytes, which
# fits a single CAN frame; a second key would take two. Lowest priority, so the
# volume never delays navigation data.
_BANDG_RAW_KEYS = {
    "bandg.navigation.rawSpeedThroughWater": 0x42,   # Boatspeed (Raw)
    "bandg.navigation.rawHeading":           0x4A,   # Heading (Raw)
    "bandg.wind.rawSpeedApparent":           0x4E,   # Apparent Wind Speed (Raw)
    "bandg.wind.rawAngleApparent":           0x52,   # Apparent Wind Angle (Raw)
}
_BANDG_HEADER = {"manufacturerCode": 381, "reserved_11": 3, "industryCode": 4}   # 7D 99


def _bandg_raw(path, key):
    """The trigger that sends ``path`` as B&G key ``key``."""
    @emits(130824, full_rate=True)
    def process_bandg_raw():
        value = get_live_data(path)
        if value is None:
            return None
        raw = round(value)
        if not -0x8000 <= raw <= 0x7FFF:
            _warn_throttled(f"raw_range:{path}",
                            "%s = %s doesn't fit signed 16 bits — not sent", path, value)
            return None
        return _build(130824, 7, "bGKeyValueData", **_BANDG_HEADER,
                      key=key, length=2, value=raw & 0xFFFF)
    return process_bandg_raw


# ── Path → trigger map ────────────────────────────────────────────────────────
# The keys are Signal-K-style dotted path names — a naming convention pyfastnet emits
# and we key on; there is no Signal K server or protocol in this pipeline, just the
# names. Each maps to the function that builds this path's PGN; an update to one of
# these paths sends its frame. See process_channel below.
_CHANNEL_MAP = {
    "navigation.headingMagnetic":                   process_heading,
    "navigation.headingTrue":                       process_heading,
    "steering.rudderAngle":                         process_rudder,
    "steering.autopilot.state":                     process_autopilot,
    "navigation.speedThroughWater":                 process_boatspeed,
    "environment.depth.belowTransducer":            process_depth,
    "environment.wind.angleApparent":               process_apparent_wind,
    "environment.wind.angleTrueWater":              process_true_wind,
    "environment.wind.directionMagnetic":           process_twd,
    "environment.wind.directionTrue":               process_twd,
    "navigation.leewayAngle":                       process_leeway,
    "navigation.speedOverGround":                   process_cog_sog,
    "electrical.batteries.house.voltage":           process_battery,
    "navigation.attitude.roll":                     process_attitude,
    "navigation.log":                               process_distance_log,
    "environment.water.temperature":                process_sea_temp,
    "environment.outside.temperature":              process_air_temp,
    "navigation.position":                          process_position,
    "environment.outside.pressure":                 process_pressure,
    "navigation.rateOfTurn":                        process_rate_of_turn,
    "navigation.courseGreatCircle.crossTrackError": process_xte,
    "environment.current.setMagnetic":              process_set_drift,
    "environment.current.setTrue":                  process_set_drift,
    **{path: _bandg_raw(path, key) for path, key in _BANDG_RAW_KEYS.items()},
}

# Paths we decode but deliberately do NOT trigger on, because a path in _CHANNEL_MAP
# already sends the PGN that carries them. Wind speed and wind angle share a single
# 130306 frame, for instance, so triggering on both would transmit it twice.
#
# The point of listing them is to separate "handled elsewhere" from "not transmitted
# at all". pyfastnet decodes about twenty more standard paths than we map — autopilot
# detail, waypoint and performance/racing data — and those are simply absent from both
# dicts.
# The text is the reason, shown at DEBUG by trigger_n2k_frame.
_SENT_WITH_ANOTHER_PATH = {
    "environment.wind.speedApparent":      "sent with environment.wind.angleApparent",
    "environment.wind.speedTrue":          "sent with the true-wind angle/direction",
    "navigation.courseOverGroundTrue":     "sent with navigation.speedOverGround",
    "navigation.courseOverGroundMagnetic": "sent with navigation.speedOverGround",
    "navigation.attitude.pitch":           "sent with navigation.attitude.roll",
    "navigation.trip.log":                 "sent with navigation.log",
    "environment.current.drift":           "sent with environment.current.set*",
    "steering.autopilot.target.headingMagnetic": "sent with steering.autopilot.state",
}

# PGNs this node can transmit, derived from the @emits tags so adding or retiring a
# trigger can't leave a stale hand-written list behind. Use tx_pgns() for what we
# actually advertise — that subtracts anything suppressed with --ignore-pgn.
TX_PGNS = sorted({trigger.pgn for trigger in _CHANNEL_MAP.values()})


def tx_pgns() -> list:
    """The PGNs to advertise to the bus: everything we can transmit, minus the ones
    suppressed by --ignore-pgn. Don't claim to transmit what we will never send."""
    return [pgn for pgn in TX_PGNS if pgn not in _ignored_pgns]


def trigger_n2k_frame(path):
    """Build (without sending) the message ``path`` would emit, or None.
    Not on the live send path — used by the tests and handy for debugging.
    Deliberately ignores --ignore-pgn: this answers "what would this path build?",
    which stays useful while diagnosing a PGN you have suppressed."""
    trigger = _CHANNEL_MAP.get(path)
    if trigger is not None:
        return trigger()

    if path in _SENT_WITH_ANOTHER_PATH:
        logger.debug("No trigger for %r — %s", path, _SENT_WITH_ANOTHER_PATH[path])
    else:
        logger.debug("No trigger for %r", path)
    return None


async def process_channel(path):
    """Build and transmit the path's frame on every update, debounced only.

    A path we don't transmit on is skipped — either it rides along with another
    path's frame (_SENT_WITH_ANOTHER_PATH), we simply don't map it, or its PGN was
    suppressed with --ignore-pgn (checked from the @emits tag before building, so a
    suppressed path costs nothing per update). Every update
    is sent — a repeated value is still live data worth putting on the bus — subject
    to MIN_SEND_INTERVAL, which caps any one path's rate (~20 Hz) so a fast-updating
    path can't flood the bus or CPU. Triggers tagged ``full_rate`` skip the cap.
    ``_channel_last_sent`` holds the monotonic time of each path's last send.
    """
    trigger = _CHANNEL_MAP.get(path)
    if trigger is None or trigger.pgn in _ignored_pgns:
        return

    now = time.monotonic()
    last_time = _channel_last_sent.get(path)
    if (not trigger.full_rate and last_time is not None
            and now - last_time < MIN_SEND_INTERVAL):
        return

    try:
        msg = trigger()
    except Exception as exc:   # noqa: BLE001 — one bad value mustn't kill the bridge
        _warn_throttled(f"build:{path}", "Frame build failed for %r (%s) — skipping",
                        path, exc)
        return
    if msg is None:
        return
    global _not_ready_since
    if _device is None or not _device.ready:
        # Not connected / address not claimed. Retry on the next update — a claim in
        # progress, or a bus that is briefly down, recovers on its own. But if it
        # never recovers, don't read Fastnet forever transmitting nothing: after
        # NOT_READY_EXIT_AFTER of continuous not-ready-with-data, give up so systemd
        # restarts us (the nmea2000 claim task is not retried if it dies).
        if _not_ready_since is None:
            _not_ready_since = now
        elif now - _not_ready_since >= NOT_READY_EXIT_AFTER:
            raise DeviceNotReadyError(
                f"CAN device not ready for {NOT_READY_EXIT_AFTER:.0f}s while frames "
                f"were waiting — address claim has likely failed")
        _warn_throttled("not_ready", "Dropping decoded frames: CAN device not ready "
                                     "(address not claimed) — will keep retrying")
        return
    _not_ready_since = None   # ready again — reset the clock
    _channel_last_sent[path] = now
    try:
        await _device.send(msg)
    except Exception as exc:   # noqa: BLE001 — bus-off / interface drop / reconnect
        # The N2KDevice client reconnects underneath; don't let a transient CAN
        # failure tear down the bridge.
        _warn_throttled("send_failed",
                        "CAN send failed (%s) — continuing; the device will reconnect",
                        exc)
