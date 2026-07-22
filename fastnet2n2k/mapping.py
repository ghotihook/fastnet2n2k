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
  which caps any one path's rate. When the source stops, output stops and consumers
  time the PGN out themselves.
"""

import logging
import time
from datetime import datetime, timezone
from math import tau

import nmea2000.pgns as pgns

from .live_store import get_live_data

logger = logging.getLogger("fastnet2n2k.mapping")

MIN_SEND_INTERVAL = 0.05    # per-path rate cap (~20 Hz); debounce only

_channel_last_sent: dict = {}
_device = None
_priority_override = None
_last_send_error_log = 0.0
_last_not_ready_log = 0.0
_SEND_ERROR_LOG_INTERVAL = 5.0


def set_device(device) -> None:
    """Register the N2KDevice that frames are transmitted through."""
    global _device
    _device = device


def set_priority_override(priority) -> None:
    """Force every transmitted frame to ``priority`` (0–7), ignoring the per-PGN
    standard priorities. ``None`` restores the standard per-PGN behaviour."""
    global _priority_override
    _priority_override = priority


def _build(pgn, priority, **fields):
    """Build an NMEA2000Message for ``pgn`` with the given field id → SI value pairs.

    ``None`` values are written through as "data not available". Source is left 0 so
    the N2KDevice substitutes its claimed address.
    """
    msg = getattr(pgns, f"decode_pgn_{pgn}")(0, 0)
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


def process_apparent_wind():
    return _wind("environment.wind.angleApparent",
                 "environment.wind.speedApparent", "Apparent")


def process_true_wind():
    return _wind("environment.wind.angleTrueWater",
                 "environment.wind.speedTrue", "True (boat referenced)")


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


def process_boatspeed():
    bs = get_live_data("navigation.speedThroughWater")   # m/s
    if bs is None:
        return None
    return _build(128259, 2, speedWaterReferenced=bs,
                  speedGroundReferenced=None, speedDirection=None)


def process_depth():
    dm = get_live_data("environment.depth.belowTransducer")   # m
    if dm is None:
        return None
    return _build(128267, 3, depth=dm, offset=None, range=None)


def process_rudder():
    ra = get_live_data("steering.rudderAngle")   # rad
    if ra is None:
        return None
    return _build(127245, 2, instance=0, position=ra, angleOrder=None)


def process_leeway():
    lw = get_live_data("navigation.leewayAngle")   # rad
    if lw is None:
        return None
    return _build(128000, 4, leewayAngle=lw)


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


def process_battery():
    v = get_live_data("electrical.batteries.house.voltage")
    if v is None:
        return None
    return _build(127508, 6, instance=0, voltage=v, current=None, temperature=None)


def process_attitude():
    roll = get_live_data("navigation.attitude.roll")     # rad
    pitch = get_live_data("navigation.attitude.pitch")   # rad
    if roll is None and pitch is None:
        return None
    return _build(127257, 3, yaw=None, pitch=pitch, roll=roll)


def process_pressure():
    bp = get_live_data("environment.outside.pressure")   # Pa
    if bp is None:
        return None
    return _build(130314, 5, instance=0, source="Atmospheric", pressure=bp)


def process_sea_temp():
    k = get_live_data("environment.water.temperature")   # Kelvin
    if k is None:
        return None
    return _build(130312, 5, instance=0, source="Sea Temperature",
                  actualTemperature=k, setTemperature=None)


def process_air_temp():
    k = get_live_data("environment.outside.temperature")   # Kelvin
    if k is None:
        return None
    return _build(130312, 5, instance=0, source="Outside Temperature",
                  actualTemperature=k, setTemperature=None)


def process_distance_log():
    stored = get_live_data("navigation.log")        # m
    trip = get_live_data("navigation.trip.log")     # m
    if stored is None and trip is None:
        return None
    now = datetime.now(timezone.utc)
    return _build(128275, 6, date=now.date(), time=now.time(),
                  log=int(stored) if stored is not None else None,
                  tripLog=int(trip) if trip is not None else None)


def process_xte():
    xte = get_live_data("navigation.courseGreatCircle.crossTrackError")   # m
    if xte is None:
        return None
    return _build(129283, 3, xteMode="Autonomous", navigationTerminated="No", xte=xte)


def process_rate_of_turn():
    yr = get_live_data("navigation.rateOfTurn")   # rad/s
    if yr is None:
        return None
    return _build(127251, 2, rate=yr)


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


def process_position():
    pos = get_live_data("navigation.position")   # {"latitude", "longitude"} degrees
    if not pos:
        return None
    return _build(129025, 2, latitude=pos["latitude"], longitude=pos["longitude"])


# ── Path → trigger map ────────────────────────────────────────────────────────
_CHANNEL_MAP = {
    "navigation.headingMagnetic":                   process_heading,
    "navigation.headingTrue":                       process_heading,
    "steering.rudderAngle":                         process_rudder,
    "navigation.speedThroughWater":                 process_boatspeed,
    "environment.depth.belowTransducer":            process_depth,
    "environment.wind.angleApparent":               process_apparent_wind,
    "environment.wind.speedApparent":               "covered by angleApparent (same frame)",
    "environment.wind.angleTrueWater":              process_true_wind,
    "environment.wind.speedTrue":                   "covered by TWA/TWD (same frame)",
    "environment.wind.directionMagnetic":           process_twd,
    "environment.wind.directionTrue":               process_twd,
    "navigation.leewayAngle":                       process_leeway,
    "navigation.speedOverGround":                   process_cog_sog,
    "navigation.courseOverGroundTrue":              "covered by speedOverGround (same frame)",
    "navigation.courseOverGroundMagnetic":          "covered by speedOverGround (same frame)",
    "electrical.batteries.house.voltage":           process_battery,
    "navigation.attitude.roll":                     process_attitude,
    "navigation.attitude.pitch":                    "covered by attitude.roll (same frame)",
    "navigation.log":                               process_distance_log,
    "navigation.trip.log":                          "covered by navigation.log (same frame)",
    "environment.water.temperature":                process_sea_temp,
    "environment.outside.temperature":              process_air_temp,
    "navigation.position":                          process_position,
    "environment.outside.pressure":                 process_pressure,
    "navigation.rateOfTurn":                        process_rate_of_turn,
    "navigation.courseGreatCircle.crossTrackError": process_xte,
    "environment.current.setMagnetic":              process_set_drift,
    "environment.current.setTrue":                  process_set_drift,
    "environment.current.drift":                    "covered by current.set* (same frame)",
}

# PGNs this node transmits — advertised to the bus by the N2KDevice.
TX_PGNS = [127245, 127250, 127251, 127257, 127508, 128000, 128259, 128267,
           128275, 129025, 129026, 129283, 129291, 130306, 130312, 130314]


def trigger_n2k_frame(path):
    entry = _CHANNEL_MAP.get(path)
    if callable(entry):
        return entry()
    if isinstance(entry, str):
        logger.debug("No trigger for %r — %s", path, entry)
    else:
        logger.debug("No trigger for %r", path)
    return None


async def process_channel(path):
    """Build and transmit the path's frame on every update, debounced only.

    Paths with no trigger (sentinel-string or unknown entries) are skipped. Every
    update is sent — a repeated value is still live data worth putting on the bus —
    subject to MIN_SEND_INTERVAL, which caps any one path's rate (~20 Hz) so a
    fast-updating path can't flood the bus or CPU. ``_channel_last_sent`` holds the
    monotonic time of each path's last send.
    """
    if not callable(_CHANNEL_MAP.get(path)):
        return

    now = time.monotonic()
    last_time = _channel_last_sent.get(path)
    if last_time is not None and now - last_time < MIN_SEND_INTERVAL:
        return

    try:
        msg = trigger_n2k_frame(path)
    except Exception as exc:   # noqa: BLE001 — one bad value mustn't kill the bridge
        logger.warning("Frame build failed for %r (%s) — skipping", path, exc)
        return
    if msg is None:
        return
    if _device is None or not _device.ready:
        # Not connected / address not claimed — retry on the next update. Never drop
        # silently: the nmea2000 library's address-claim runs in a background task
        # that is not retried if it dies, so a bridge stuck not-ready would otherwise
        # read Fastnet happily while transmitting nothing, with no trace in the logs.
        global _last_not_ready_log
        if now - _last_not_ready_log >= _SEND_ERROR_LOG_INTERVAL:
            logger.warning("Dropping decoded frames: CAN device not ready "
                           "(address not claimed) — will keep retrying")
            _last_not_ready_log = now
        return
    _channel_last_sent[path] = now
    try:
        await _device.send(msg)
    except Exception as exc:   # noqa: BLE001 — bus-off / interface drop / reconnect
        # The N2KDevice client reconnects underneath; don't let a transient CAN
        # failure tear down the bridge. Log at most once per interval to avoid
        # flooding while the bus is down.
        global _last_send_error_log
        if now - _last_send_error_log >= _SEND_ERROR_LOG_INTERVAL:
            logger.warning("CAN send failed (%s) — continuing; the device will reconnect",
                           exc)
            _last_send_error_log = now
