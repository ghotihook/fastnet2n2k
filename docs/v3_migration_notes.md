# v3 Migration Notes — adapting fastnet2n2k to pyfastnet v3

Companion to pyfastnet's `docs/v3_signalk_mapping.md`. Describes what changes in this
project when pyfastnet moves to its v3 output.

> **Status: done.** This was written as a plan; the migration shipped in `e0cc341`
> (v3.0.0) and the notes are kept as background on *why* the mapping looks the way it
> does. Read it as history — where it says "current" or "today" it means pre-v3.
> The predicted collapse of the unit-variant channels did happen: pyfastnet now
> projects °F, Feet and Fathoms onto the same paths as °C and Meters, so there are no
> duplicate-unit entries left in `_CHANNEL_MAP`. Paths that a *different* trigger
> already covers now live in `_SENT_WITH_ANOTHER_PATH` rather than as string
> sentinels inside `_CHANNEL_MAP`.

## What changes upstream (pyfastnet v3)

The decoder stops emitting per-channel `{value, display_text, layout}` keyed by
human-readable channel name, and instead emits a flat map:

```
frame["values"] = { "<signalk.path>": <SI value>, ... }
```

- **Keys** are Signal K paths (`navigation.speedThroughWater`, …), not names.
- **Values** are already SI (radians, m/s, Kelvin, Pascals, metres) — the same units
  N2K uses. Value is `float`, or `str` for enums, or `{"latitude","longitude"}` for
  position, or `None` when unavailable.
- **`display_text` and `layout` are gone** — never exposed outside the decoder.
- **True/Magnetic is in the path** (`headingMagnetic` vs `headingTrue`,
  `courseOverGroundTrue/Magnetic`, `current.setTrue/setMagnetic`), not in `layout`.
- **Position** is parsed by the decoder into `navigation.position`.

## Headline: this consumer becomes a near pass-through

Because **N2K SI == Signal K SI**, almost every transform in `mapping.py` is now done
upstream. The migration is mostly *deletion*: strip our unit conversions, delete the
layout/T-M plumbing, rekey to paths. The PGN-building and multi-channel aggregation
logic stays.

## Required edits by file

### `fastnet2n2k/live_store.py`
- `update_live_data(...)` — drop the `display_text` and `layout` params; store just
  `{value, timestamp}` keyed by **path**.
- **Delete `get_live_display`** — its only caller (`process_position`) changes (below).

### `fastnet2n2k/__main__.py` (feed loop ~L105–107)
- `update_live_data(name, decoded["channel_id"], decoded["value"], decoded["display_text"], decoded["layout"])`
  → `update_live_data(path, value)` iterating the new `{path: value}` map.

### `fastnet2n2k/mapping.py` — the bulk of the work
**Delete all unit conversions** (values now arrive SI):
- `KN_MS` constant and every `* KN_MS` (boatspeed, SOG, wind speed, drift).
- every `radians(...)` (heading, rudder, leeway, attitude, yaw, COG, set, wind angle).
- `_c_to_k` / `_f_to_k` and the dual-unit `_temperature` helper.
- `* 100` (pressure), `* 1852` (log, XTE).
- degree wraps: `if angle < 0: angle += 360` and `% 360` → radians (`+= 2*pi`, `% (2*pi)`),
  or drop if the decoder already normalises.

⚠️ **Silent-corruption risk:** a *missed* conversion is not a crash — it's
`radians(already-radians)` on the bus. Grep for `radians`, `KN_MS`, `_to_k`, `* 100`,
`* 1852`, `% 360`, `+= 360` and clear every one.

**Delete the T/M-from-layout plumbing** — reference now comes from the path:
- `_LAYOUT_BEARING_REF`, `_LAYOUT_WIND_REF`, `_bearing_ref()`.
- `process_heading` / `process_set_drift` / `process_twd`: pick the N2K `reference`
  field from *which path* triggered (Magnetic vs True), not from a layout lookup.
- `process_cog_sog`: `cogReference` from which COG path is present (already two channels).

**Rewrite `process_position`**: consume `navigation.position` →
`{"latitude","longitude"}` directly; delete the ASCII/`get_live_display` parsing.

**Rekey `_CHANNEL_MAP`** to paths (see table below), and key triggers by path.

### `fastnet2n2k/display.py` (`print_live_data`)
- No `display_text` available. Either format a display string from `value` + path, or
  simplify the live console to `path = value`.

### `tests/`
- `test_mapping.py`, `test_pipeline.py` assert on old names/units — rekey to paths and
  drop the expected-unit conversions.

## Channel rekey table (channels this bridge actually uses)

| Old name | v3 Signal K path | N2K trigger |
|----------|------------------|-------------|
| Heading | `navigation.headingMagnetic` \| `headingTrue` | 127250 |
| Rudder Angle | `steering.rudderAngle` | 127245 |
| Boatspeed (Knots) | `navigation.speedThroughWater` | 128259 |
| Depth (Meters) | `environment.depth.belowTransducer` | 128267 |
| Apparent Wind Angle | `environment.wind.angleApparent` | 130306 |
| Apparent Wind Speed (Knots) | `environment.wind.speedApparent` | 130306 |
| True Wind Angle | `environment.wind.angleTrueWater` | 130306 |
| True Wind Speed (Knots) | `environment.wind.speedTrue` | 130306 |
| True Wind Direction | `environment.wind.directionMagnetic` \| `directionTrue` | 130306 |
| Leeway | `navigation.leewayAngle` | 128000 |
| Speed Over Ground | `navigation.speedOverGround` | 129026 |
| Course Over Ground (True) | `navigation.courseOverGroundTrue` | 129026 |
| Course Over Ground (Mag) | `navigation.courseOverGroundMagnetic` | 129026 |
| Battery Volts | `electrical.batteries.<id>.voltage` | 127508 |
| Heel Angle | `navigation.attitude.roll` | 127257 |
| Fore/Aft Trim | `navigation.attitude.pitch` | 127257 |
| Stored Log (NM) | `navigation.log` | 128275 |
| Trip Log (NM) | `navigation.trip.log` | 128275 |
| Sea Temperature (°C/°F) | `environment.water.temperature` | 130312 |
| Air Temperature (°C/°F) | `environment.outside.temperature` | 130312 |
| LatLon | `navigation.position` | 129025 |
| Barometric Pressure | `environment.outside.pressure` | 130314 |
| Yaw rate | `navigation.rateOfTurn` | 127251 |
| Cross Track Error | `navigation.courseGreatCircle.crossTrackError` | 129283 |
| Tidal Set | `environment.current.setMagnetic` \| `setTrue` | 129291 |
| Tidal Drift | `environment.current.drift` | 129291 |

Note the temperature and depth **unit-variant channels collapse to one path** upstream —
the current `_CHANNEL_MAP` duplicate/`covered-by` sentinels for °F, Feet, Fathoms, Raw,
m/s go away (pyfastnet no longer emits them).

## Handling True/Magnetic (the runtime-routing point)

pyfastnet routes T/M by **which path** it emits, decided at decode time from the layout
byte. So key `_CHANNEL_MAP` triggers on **both** `...Magnetic` and `...True` paths; the
trigger reads the reference from the path that fired. **Today pyfastnet only emits the
Magnetic paths** (the `°T` layout byte value is not yet identified — pyfastnet TBC #7),
so the `...True` entries are dormant but ready — no consumer change needed when True
lands upstream.

## What stays unchanged
- `_build()` and all PGN construction; the N2KDevice send / address-claim / reconnect.
- Multi-channel aggregation into one PGN (wind angle+speed → 130306; heel+trim → 127257;
  COG+SOG → 129026; log+trip → 128275; set+drift → 129291).
- Cadence: event-driven send + `MIN_SEND_INTERVAL` debounce.
- **Sign** — still taken directly from the decoded `value` (unchanged upstream).
- `TX_PGNS`.

## Watch-outs / upstream dependencies
- **0x06 display-only dropout (upstream, cannot fix here):** pyfastnet v3 keeps format
  0x06 as display-only (`value=None`), so channels that arrive *only* as display frames
  (seen for Heel/Trim/Target-TWA on some captures) emit nothing. On such a bus those PGNs
  go silent. Accepted upstream (pyfastnet note 12) — flag it in field testing.
- **`navigation.position` value shape** must match what pyfastnet emits (pyfastnet TBC #8).
- **`°T` code** for True bearings (pyfastnet TBC #7) — until then, True paths never arrive.
- **Depth**: pyfastnet emits `belowTransducer` via a metres→feet→fathoms fallback; if a
  keel/surface offset is configured the path may differ (pyfastnet TBC #2).

## Coordination
- Bump the pyfastnet pin to `>=3.0.0` in `pyproject.toml` and `requirements.txt`.
- Cut the fastnet2n2k release together with pyfastnet v3 (breaking on both sides).
