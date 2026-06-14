"""Map decoded Fastnet channels to NMEA2000 messages and send them on the CAN bus.

Ported from fastnet2ip/handlers/nmea2000.py, re-targeted from tomer-w's UDP encoder
to the finnboeger ``n2k`` library transmitting on a real SocketCAN interface via
``n2k.Node.send_msg``.

Conventions (per design decisions):
- **Units** are converted to NMEA2000 SI: knots→m/s, degrees→radians, °C/°F→Kelvin,
  NM→metres, mbar→Pascals. Depth is already metres.
- **Sign** is taken directly from pyfastnet's ``value`` (pyfastnet applies the
  layout-derived sign during decode).
- **T/M reference** is taken from the pyfastnet ``layout`` field — the only place it
  exists. If a bearing channel's layout doesn't map to a known reference the frame is
  skipped and logged (never guessed).
- **Staleness**: frames are sent only when a channel updates (event-driven), with a
  minimum interval and a maximum re-broadcast age. When the source stops, output
  stops and consumers time the PGN out themselves.
"""

import logging
import time
from datetime import datetime, timezone
from math import radians

from n2k import types as n2k_types
from n2k.messages import (
    Attitude,
    BatteryStatus,
    BoatSpeed,
    CogSogRapid,
    CrossTrackError,
    DistanceLog,
    Heading,
    LatLonRapid,
    Leeway,
    RateOfTurn,
    Rudder,
    Temperature,
    ActualPressure,
    WaterDepth,
    WindSpeed,
    create_n2k_attitude_message,
    create_n2k_battery_status_message,
    create_n2k_boat_speed_message,
    create_n2k_cog_sog_rapid_message,
    create_n2k_cross_track_error_message,
    create_n2k_distance_log_message,
    create_n2k_heading_message,
    create_n2k_lat_long_rapid_message,
    create_n2k_leeway_message,
    create_n2k_rate_of_turn_message,
    create_n2k_rudder_message,
    create_n2k_temperature_message,
    create_n2k_actual_pressure_message,
    create_n2k_water_depth_message,
    create_n2k_wind_speed_message,
)

from .live_store import get_live_data, get_live_display, live_data

logger = logging.getLogger("fastnet2n2k.mapping")

# ── Tuning ────────────────────────────────────────────────────────────────────
MIN_SEND_INTERVAL = 0.05   # s — never send the same channel faster than 20 Hz
REBROADCAST_AGE   = 5.0    # s — re-send an unchanged value at most this often
KN_MS             = 0.514444

_channel_last_sent: dict = {}
_sid = 0
_node = None


def set_node(node) -> None:
    """Register the n2k.Node that frames are transmitted through."""
    global _node
    _node = node


def _next_sid() -> int:
    global _sid
    _sid = (_sid + 1) % 253
    return _sid


def _c_to_k(c: float) -> float:
    return c + 273.15


def _f_to_k(f: float) -> float:
    return (f - 32) * 5 / 9 + 273.15


# ── Layout → reference (the only source of T/M) ───────────────────────────────
_LAYOUT_HEADING_REF = {
    "°M": n2k_types.N2kHeadingReference.magnetic,
    "°T": n2k_types.N2kHeadingReference.true,
}
_LAYOUT_WIND_REF = {
    "°M": n2k_types.N2kWindReference.Magnetic,
    "°T": n2k_types.N2kWindReference.TrueNorth,
}


def _heading_ref(name: str):
    entry = live_data.get(name)
    if entry is None:
        return None
    ref = _LAYOUT_HEADING_REF.get(entry["layout"])
    if ref is None:
        logger.error("%s: unrecognised layout %r — skipping frame", name, entry["layout"])
    return ref


# ── Triggers (each returns one n2k Message, or None) ──────────────────────────

def _wind(angle_ch, speed_ch, reference):
    angle = get_live_data(angle_ch)
    speed = get_live_data(speed_ch)
    if angle is None and speed is None:
        return None
    if angle is not None and angle < 0:   # N2K wind angle is 0..2π
        angle += 360
    return create_n2k_wind_speed_message(WindSpeed(
        sid=_next_sid(),
        wind_speed=speed * KN_MS if speed is not None else None,
        wind_angle=radians(angle) if angle is not None else None,
        wind_reference=reference,
    ))


def process_apparent_wind():
    return _wind("Apparent Wind Angle", "Apparent Wind Speed (Knots)",
                 n2k_types.N2kWindReference.Apparent)


def process_true_wind():
    return _wind("True Wind Angle", "True Wind Speed (Knots)",
                 n2k_types.N2kWindReference.TrueBoat)


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
    ref = _heading_ref("Heading")
    if ref is None:
        return None
    return create_n2k_heading_message(Heading(
        sid=_next_sid(), heading=radians(hdg), deviation=None, variation=None, ref=ref))


def process_boatspeed():
    bs = get_live_data("Boatspeed (Knots)")
    if bs is None:
        return None
    return create_n2k_boat_speed_message(BoatSpeed(
        sid=_next_sid(), water_referenced=bs * KN_MS, ground_referenced=None,
        swrt=n2k_types.N2kSpeedWaterReferenceType.PaddleWheel))


def process_depth():
    dm = get_live_data("Depth (Meters)")
    if dm is None:
        return None
    return create_n2k_water_depth_message(WaterDepth(
        sid=_next_sid(), depth_below_transducer=dm, offset=None, max_range=None))


def process_rudder():
    ra = get_live_data("Rudder Angle")
    if ra is None:
        return None
    return create_n2k_rudder_message(Rudder(
        instance=0, rudder_position=radians(ra), angle_order=None,
        rudder_direction_order=n2k_types.N2kRudderDirectionOrder.NoDirectionOrder))


def process_leeway():
    lw = get_live_data("Leeway")
    if lw is None:
        return None
    return create_n2k_leeway_message(Leeway(sid=_next_sid(), leeway=radians(lw)))


def process_cog_sog():
    cog_true = get_live_data("Course Over Ground (True)")
    cog_mag  = get_live_data("Course Over Ground (Mag)")
    sog      = get_live_data("Speed Over Ground")
    if sog is None:
        return None
    sog_ms = sog * KN_MS
    if cog_true is not None:
        ref, cog = n2k_types.N2kHeadingReference.true, radians(cog_true % 360)
    elif cog_mag is not None:
        ref, cog = n2k_types.N2kHeadingReference.magnetic, radians(cog_mag % 360)
    else:
        ref, cog = n2k_types.N2kHeadingReference.true, None
    return create_n2k_cog_sog_rapid_message(CogSogRapid(
        sid=_next_sid(), heading_reference=ref, cog=cog, sog=sog_ms))


def process_battery():
    v = get_live_data("Battery Volts")
    if v is None:
        return None
    return create_n2k_battery_status_message(BatteryStatus(
        sid=_next_sid(), battery_instance=0, battery_voltage=v,
        battery_current=None, battery_temperature=None))


def process_attitude():
    roll  = get_live_data("Heel Angle")
    pitch = get_live_data("Fore/Aft Trim")
    if roll is None and pitch is None:
        return None
    return create_n2k_attitude_message(Attitude(
        sid=_next_sid(), yaw=None,
        pitch=radians(pitch) if pitch is not None else None,
        roll=radians(roll) if roll is not None else None))


def process_pressure():
    bp = get_live_data("Barometric Pressure")   # mbar / hPa
    if bp is None:
        return None
    return create_n2k_actual_pressure_message(ActualPressure(
        sid=_next_sid(), pressure_instance=0,
        pressure_source=n2k_types.N2kPressureSource.Atmospheric,
        actual_pressure=bp * 100))


def _temperature(channel_c, channel_f, source):
    c = get_live_data(channel_c)
    if c is not None:
        k = _c_to_k(c)
    else:
        f = get_live_data(channel_f)
        if f is None:
            return None
        k = _f_to_k(f)
    return create_n2k_temperature_message(Temperature(
        sid=_next_sid(), temp_instance=0, temp_source=source,
        actual_temperature=k, set_temperature=None))


def process_sea_temp():
    return _temperature("Sea Temperature (°C)", "Sea Temperature (°F)",
                        n2k_types.N2kTempSource.SeaTemperature)


def process_air_temp():
    return _temperature("Air Temperature (°C)", "Air Temperature (°F)",
                        n2k_types.N2kTempSource.OutsideTemperature)


def process_distance_log():
    stored = get_live_data("Stored Log (NM)")
    trip   = get_live_data("Trip Log (NM)")
    if stored is None and trip is None:
        return None
    now = datetime.now(timezone.utc)
    secs = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    days = (now.date() - datetime(1970, 1, 1, tzinfo=timezone.utc).date()).days
    return create_n2k_distance_log_message(DistanceLog(
        days_since_1970=days, seconds_since_midnight=secs,
        log=int(stored * 1852) if stored is not None else None,
        trip_log=int(trip * 1852) if trip is not None else None))


def process_xte():
    xte = get_live_data("Cross Track Error")
    if xte is None:
        return None
    return create_n2k_cross_track_error_message(CrossTrackError(
        sid=_next_sid(), xte_mode=n2k_types.N2kXTEMode.Autonomous,
        navigation_terminated=False, xte=xte * 1852))


def process_rate_of_turn():
    yr = get_live_data("Yaw rate")
    if yr is None:
        return None
    return create_n2k_rate_of_turn_message(RateOfTurn(
        sid=_next_sid(), rate_of_turn=radians(yr)))


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
    return create_n2k_lat_long_rapid_message(LatLonRapid(latitude=lat, longitude=lon))


# ── Channel → trigger map ─────────────────────────────────────────────────────
# A callable builds a frame; a string documents why a channel has no own trigger
# (its data is carried in another channel's frame). Deferred PGNs are noted too.
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
    # ── Deferred (no n2k builder yet; need manual Message.add_*) ──
    "Tidal Set":                    "TODO: Set & Drift PGN 129291 (deferred)",
    "Tidal Drift":                  "TODO: Set & Drift PGN 129291 (deferred)",
    "Boatspeed (Raw)":             "TODO: proprietary PGN 65282 (deferred)",
    "Heading (Raw)":               "TODO: proprietary PGN 65281 (deferred)",
    "Apparent Wind Speed (Raw)":   "TODO: proprietary PGN 65280 (deferred)",
    "Apparent Wind Angle (Raw)":   "TODO: proprietary PGN 65280 (deferred)",
}


def trigger_n2k_frame(channel_name):
    entry = _CHANNEL_MAP.get(channel_name)
    if callable(entry):
        return entry()
    if isinstance(entry, str):
        logger.debug("No trigger for %r — %s", channel_name, entry)
    else:
        logger.debug("No trigger for %r", channel_name)
    return None


def process_channel(channel_name, old_entry):
    """Apply the throttle policy, then build and transmit the channel's frame."""
    now = time.monotonic()
    current = live_data.get(channel_name)
    new_key = (current["value"], current["display_text"]) if current else (None, None)
    old_key = (old_entry["value"], old_entry["display_text"]) if old_entry else (None, None)

    last_sent = _channel_last_sent.get(channel_name)
    if last_sent is not None:
        if (now - last_sent) < MIN_SEND_INTERVAL:
            return
        if new_key == old_key and (now - last_sent) < REBROADCAST_AGE:
            return

    msg = trigger_n2k_frame(channel_name)
    if msg is None:
        return
    _channel_last_sent[channel_name] = now
    if _node is not None:
        _node.send_msg(msg)
