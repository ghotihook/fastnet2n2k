"""Map decoded Fastnet channels to NMEA2000 messages and send them on the CAN bus.

Uses the ``nmea2000`` library (tomer-w), which is canboat-based: messages are built
by taking a blank PGN template from :mod:`nmea2000.pgns`, setting the fields we care
about (by id, in SI units), and handing the resulting ``NMEA2000Message`` to an
``N2KDevice`` that transmits it over SocketCAN and manages ISO address claiming.

Conventions:
- **Units** → NMEA2000 SI: knots→m/s, degrees→radians, °C/°F→Kelvin, NM→m, mbar→Pa.
- **Sign** is taken directly from pyfastnet's decoded ``value``.
- **T/M reference** is taken from the pyfastnet ``layout`` field (the only place it
  exists) and mapped to the canboat lookup string. If a bearing channel's layout
  can't be resolved the frame is skipped and logged — never guessed.
- **Cadence**: a PGN is sent on every channel update (event-driven) — a repeated
  value is still live data worth putting on the bus — debounced only by
  MIN_SEND_INTERVAL, which caps any one channel's rate. When the source stops, output
  stops and consumers time the PGN out themselves.
"""

import logging
import time
from datetime import datetime, timezone
from math import radians

import nmea2000.pgns as pgns

from .live_store import get_live_data, get_live_display, live_data

logger = logging.getLogger("fastnet2n2k.mapping")

MIN_SEND_INTERVAL = 0.05    # per-channel rate cap (~20 Hz); debounce only
KN_MS             = 0.514444

_channel_last_sent: dict = {}
_device = None
_priority_override = None
_last_send_error_log = 0.0
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


def _c_to_k(c):
    return c + 273.15


def _f_to_k(f):
    return (f - 32) * 5 / 9 + 273.15


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


# ── Layout → canboat reference string (the only source of T/M) ────────────────
_LAYOUT_BEARING_REF = {"°M": "Magnetic", "°T": "True"}
_LAYOUT_WIND_REF = {
    "°M": "Magnetic (ground referenced to Magnetic North)",
    "°T": "True (ground referenced to North)",
}


def _bearing_ref(name):
    entry = live_data.get(name)
    if entry is None:
        return None
    ref = _LAYOUT_BEARING_REF.get(entry["layout"])
    if ref is None:
        logger.error("%s: unrecognised layout %r — skipping frame", name, entry["layout"])
    return ref


# ── Triggers: each returns one NMEA2000Message, or None ───────────────────────

def _wind(angle_ch, speed_ch, reference):
    angle = get_live_data(angle_ch)
    speed = get_live_data(speed_ch)
    if angle is None and speed is None:
        return None
    if angle is not None and angle < 0:        # N2K wind angle is 0..2π
        angle += 360
    return _build(130306, 2,
                  windSpeed=speed * KN_MS if speed is not None else None,
                  windAngle=radians(angle) if angle is not None else None,
                  reference=reference)


def process_apparent_wind():
    return _wind("Apparent Wind Angle", "Apparent Wind Speed (Knots)", "Apparent")


def process_true_wind():
    return _wind("True Wind Angle", "True Wind Speed (Knots)", "True (boat referenced)")


def process_twd():
    entry = live_data.get("True Wind Direction")
    if entry is None:
        return None
    ref = _LAYOUT_WIND_REF.get(entry["layout"])
    if ref is None:
        logger.error("True Wind Direction: unrecognised layout %r — skipping frame",
                     entry["layout"])
        return None
    return _wind("True Wind Direction", "True Wind Speed (Knots)", ref)


def process_heading():
    hdg = get_live_data("Heading")
    if hdg is None:
        return None
    ref = _bearing_ref("Heading")
    if ref is None:
        return None
    return _build(127250, 2, heading=radians(hdg), reference=ref,
                  deviation=None, variation=None)


def process_boatspeed():
    bs = get_live_data("Boatspeed (Knots)")
    if bs is None:
        return None
    return _build(128259, 2, speedWaterReferenced=bs * KN_MS,
                  speedGroundReferenced=None, speedDirection=None)


def process_depth():
    dm = get_live_data("Depth (Meters)")
    if dm is None:
        return None
    return _build(128267, 3, depth=dm, offset=None, range=None)


def process_rudder():
    ra = get_live_data("Rudder Angle")
    if ra is None:
        return None
    return _build(127245, 2, instance=0, position=radians(ra), angleOrder=None)


def process_leeway():
    lw = get_live_data("Leeway")
    if lw is None:
        return None
    return _build(128000, 4, leewayAngle=radians(lw))


def process_cog_sog():
    cog_true = get_live_data("Course Over Ground (True)")
    cog_mag  = get_live_data("Course Over Ground (Mag)")
    sog      = get_live_data("Speed Over Ground")
    if sog is None:
        return None
    sog_ms = sog * KN_MS
    if cog_true is not None:
        return _build(129026, 2, cogReference="True",
                      cog=radians(cog_true % 360), sog=sog_ms)
    if cog_mag is not None:
        return _build(129026, 2, cogReference="Magnetic",
                      cog=radians(cog_mag % 360), sog=sog_ms)
    return _build(129026, 2, cog=None, sog=sog_ms)


def process_battery():
    v = get_live_data("Battery Volts")
    if v is None:
        return None
    return _build(127508, 6, instance=0, voltage=v, current=None, temperature=None)


def process_attitude():
    roll  = get_live_data("Heel Angle")
    pitch = get_live_data("Fore/Aft Trim")
    if roll is None and pitch is None:
        return None
    return _build(127257, 3, yaw=None,
                  pitch=radians(pitch) if pitch is not None else None,
                  roll=radians(roll) if roll is not None else None)


def process_pressure():
    bp = get_live_data("Barometric Pressure")   # mbar / hPa
    if bp is None:
        return None
    return _build(130314, 5, instance=0, source="Atmospheric", pressure=bp * 100)


def _temperature(channel_c, channel_f, source):
    c = get_live_data(channel_c)
    if c is not None:
        k = _c_to_k(c)
    else:
        f = get_live_data(channel_f)
        if f is None:
            return None
        k = _f_to_k(f)
    return _build(130312, 5, instance=0, source=source,
                  actualTemperature=k, setTemperature=None)


def process_sea_temp():
    return _temperature("Sea Temperature (°C)", "Sea Temperature (°F)", "Sea Temperature")


def process_air_temp():
    return _temperature("Air Temperature (°C)", "Air Temperature (°F)", "Outside Temperature")


def process_distance_log():
    stored = get_live_data("Stored Log (NM)")
    trip   = get_live_data("Trip Log (NM)")
    if stored is None and trip is None:
        return None
    now = datetime.now(timezone.utc)
    return _build(128275, 6, date=now.date(), time=now.time(),
                  log=int(stored * 1852) if stored is not None else None,
                  tripLog=int(trip * 1852) if trip is not None else None)


def process_xte():
    xte = get_live_data("Cross Track Error")
    if xte is None:
        return None
    return _build(129283, 3, xteMode="Autonomous", navigationTerminated="No",
                  xte=xte * 1852)


def process_rate_of_turn():
    yr = get_live_data("Yaw rate")
    if yr is None:
        return None
    return _build(127251, 2, rate=radians(yr))


def process_set_drift():
    set_deg = get_live_data("Tidal Set")     # degrees
    drift   = get_live_data("Tidal Drift")   # knots
    if set_deg is None and drift is None:
        return None
    ref = _bearing_ref("Tidal Set")
    if ref is None:
        return None
    return _build(129291, 3, setReference=ref,
                  set=radians(set_deg % 360) if set_deg is not None else None,
                  drift=max(0.0, drift) * KN_MS if drift is not None else None)


def process_position():
    latlon = get_live_display("LatLon")          # e.g. "3352.450S15113.920E"
    if not latlon:
        return None
    lat_idx = latlon.find('N') if 'N' in latlon else latlon.find('S')
    lon_idx = latlon.find('E') if 'E' in latlon else latlon.find('W')
    if lat_idx == -1 or lon_idx == -1:
        return None
    try:
        lat_part, lat_dir = latlon[:lat_idx], latlon[lat_idx]
        lon_part, lon_dir = latlon[lat_idx + 1:lon_idx], latlon[lon_idx]
        lat = int(lat_part[:2]) + float(lat_part[2:]) / 60
        lon = int(lon_part[:3]) + float(lon_part[3:]) / 60
    except (ValueError, IndexError):
        logger.debug("position: could not parse %r", latlon)
        return None
    if lat_dir == 'S':
        lat = -lat
    if lon_dir == 'W':
        lon = -lon
    return _build(129025, 2, latitude=lat, longitude=lon)


# ── Channel → trigger map ─────────────────────────────────────────────────────
_CHANNEL_MAP = {
    "Heading":                      process_heading,
    "Rudder Angle":                 process_rudder,
    "Boatspeed (Knots)":            process_boatspeed,
    "Depth (Meters)":               process_depth,
    "Depth (Feet)":                 "duplicate of Depth (Meters)",
    "Depth (Fathoms)":              "duplicate of Depth (Meters)",
    "Apparent Wind Angle":          process_apparent_wind,
    "Apparent Wind Speed (Knots)":  "covered by 'Apparent Wind Angle' (same frame)",
    "True Wind Angle":              process_true_wind,
    "True Wind Direction":          process_twd,
    "True Wind Speed (Knots)":      "covered by True Wind Angle/Direction (same frame)",
    "True Wind Speed (m/s)":        "covered by True Wind Angle/Direction (same frame)",
    "Leeway":                       process_leeway,
    "Speed Over Ground":            process_cog_sog,
    "Course Over Ground (True)":    "covered by 'Speed Over Ground' (same frame)",
    "Course Over Ground (Mag)":     "covered by 'Speed Over Ground' (same frame)",
    "Battery Volts":                process_battery,
    "Heel Angle":                   process_attitude,
    "Fore/Aft Trim":                "covered by 'Heel Angle' (same frame)",
    "Stored Log (NM)":              process_distance_log,
    "Trip Log (NM)":                "covered by 'Stored Log (NM)' (same frame)",
    "Sea Temperature (°C)":         process_sea_temp,
    "Sea Temperature (°F)":         process_sea_temp,
    "Air Temperature (°C)":         process_air_temp,
    "Air Temperature (°F)":         process_air_temp,
    "LatLon":                       process_position,
    "Barometric Pressure":          process_pressure,
    "Yaw rate":                     process_rate_of_turn,
    "Cross Track Error":            process_xte,
    "Tidal Set":                    process_set_drift,
    "Tidal Drift":                  "covered by 'Tidal Set' (same frame)",
    # Deferred: B&G proprietary raw PGNs (65280-65282) — manufacturer-specific.
    "Boatspeed (Raw)":             "TODO: proprietary PGN 65282 (deferred)",
    "Heading (Raw)":               "TODO: proprietary PGN 65281 (deferred)",
    "Apparent Wind Speed (Raw)":   "TODO: proprietary PGN 65280 (deferred)",
    "Apparent Wind Angle (Raw)":   "TODO: proprietary PGN 65280 (deferred)",
}

# PGNs this node transmits — advertised to the bus by the N2KDevice.
TX_PGNS = [127245, 127250, 127251, 127257, 127508, 128000, 128259, 128267,
           128275, 129025, 129026, 129283, 129291, 130306, 130312, 130314]


def trigger_n2k_frame(channel_name):
    entry = _CHANNEL_MAP.get(channel_name)
    if callable(entry):
        return entry()
    if isinstance(entry, str):
        logger.debug("No trigger for %r — %s", channel_name, entry)
    else:
        logger.debug("No trigger for %r", channel_name)
    return None


async def process_channel(channel_name):
    """Build and transmit the channel's frame on every update, debounced only.

    Channels with no trigger (sentinel-string or unknown entries) are skipped. Every
    update is sent — a repeated value is still live data worth putting on the bus —
    subject to MIN_SEND_INTERVAL, which caps any one channel's rate (~20 Hz) so a
    fast-updating channel can't flood the bus or CPU. ``_channel_last_sent`` holds the
    monotonic time of each channel's last send.
    """
    if not callable(_CHANNEL_MAP.get(channel_name)):
        return

    now = time.monotonic()
    last_time = _channel_last_sent.get(channel_name)
    if last_time is not None and now - last_time < MIN_SEND_INTERVAL:
        return

    msg = trigger_n2k_frame(channel_name)
    if msg is None:
        return
    if _device is None or not _device.ready:
        return   # not connected / address not claimed — retry on the next update
    _channel_last_sent[channel_name] = now
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
