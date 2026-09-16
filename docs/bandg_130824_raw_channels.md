# B&G key-value data on NMEA 2000 (PGN 130824)

**Status: raw sensor channels shipped in 3.4.0; performance channels in 3.5.0.**
fastnet2n2k sends two groups of Fastnet data onto the NMEA 2000 bus as B&G proprietary
key-value data: the four raw (uncalibrated) sensor channels, for logging, and VMG and
next-tack heading, for display. This note describes the frame byte by byte, explains
why it takes this form, and gives a decoder. The code is `_bandg_raw` and
`_bandg_perf` in [`fastnet2n2k/mapping.py`](../fastnet2n2k/mapping.py).

## What is sent

| Key | Fastnet channel | pyfastnet path |
|---|---|---|
| 66 (0x42) | Boatspeed (Raw) | `bandg.navigation.rawSpeedThroughWater` |
| 74 (0x4A) | Heading (Raw) | `bandg.navigation.rawHeading` |
| 78 (0x4E) | Apparent Wind Speed (Raw) | `bandg.wind.rawSpeedApparent` |
| 82 (0x52) | Apparent Wind Angle (Raw) | `bandg.wind.rawAngleApparent` |

These are the sensor readings before the H2000 applies its calibration: paddlewheel,
compass and masthead unit as they report. They are sent so the calibration can be
refitted later from logged data. They are not for display, and nothing on the bus
uses them live.

## Why a proprietary frame

NMEA 2000 has no standard PGN for uncalibrated sensor counts. It does set aside PGN
ranges for manufacturer-specific data:

| Range | Type |
|---|---|
| 61184 (0xEF00) | single frame, addressed |
| 65280–65535 (0xFF00–0xFFFF) | single frame, broadcast |
| 126720 (0x1EF00) | fast-packet, addressed |
| 130816–131071 (0x1FF00–0x1FFFF) | fast-packet, broadcast |

Every proprietary payload must start with a 2-byte header: an 11-bit **manufacturer
code**, 2 reserved bits, and a 3-bit **industry code** (4 = marine). The manufacturer
code says whose definition the rest of the payload follows. That is how one PGN number
can carry unrelated messages from different vendors: 65280 is Furuno heave, Maretron
keel position and CZone circuit control at once. A receiver that doesn't recognise the
manufacturer code ignores the message.

## Why B&G's format rather than our own

**B&G doesn't publish its NMEA 2000 formats.** Navico/B&G's public developer
interface, GoFree, covers the Ethernet websocket that MFDs and Expedition use, not
NMEA 2000. Its Tier 2 WebSocket document contains no raw or uncalibrated data
items [6].

**B&G's own proprietary PGN is 130824, "B&G: key-value data".** canboat has
reverse-engineered it from captures [1][3]. After the manufacturer header it carries
any number of entries, each a 12-bit key, a 4-bit byte length, and the value.

**B&G's keys are Fastnet channel numbers.** canboat's key table [2] matches the
Fastnet channel map for keys 0–255:

| Key | canboat name | Fastnet channel |
|---|---|---|
| 11 | Rudder Angle | 0x0B Rudder Angle |
| 65 | Water Speed | 0x41 Boatspeed (Knots) |
| 83 | Target TWA | 0x53 Target TWA |
| 124 | Polar Performance | 0x7C Polar Performance |
| 127 | VMG to Wind | 0x7F Velocity Made Good |
| 156 | Mast Angle | 0x9C Mast Angle |
| 239–248 | Remote 0–9 | 0xEF–0xF8 Remote 0–9 |

Keys 256 and up are additions from B&G's newer processors, such as start-line and
layline data. In effect, B&G processors carry Fastnet channels onto NMEA 2000 in this
PGN.

**B&G gear has never been seen sending the raw keys.** Keys 66, 74, 78 and 82 are
missing from canboat's key table. canboat's B&G sample captures [4] contain 29
distinct 130824 keys, all performance and navigation data. The H5000 appears to keep
raw values internal; H5000 data loggers work over the websocket instead [6].

**So the frame is what a B&G processor would send if it sent raw data:** B&G's
layout, with each raw channel under its own Fastnet channel number. Nothing is
invented except the value format for these four keys (below). The alternatives were
worse:

- **An invented format under fastnet2n2k's own manufacturer code** (2046, which the
  bridge uses in its address claim) would be cleaner on ownership, but no tool would
  recognise it.
- **The earlier 65280–65282 design** had several defects; see [History](#history).

The trade-off is that the frames carry B&G's manufacturer code, 381, while the bridge
identifies itself on the bus as 2046. The code in a proprietary message names whose
*format* the payload follows, and this is B&G instrument data in B&G's layout. The
remaining risks are small:

- **A real B&G NMEA 2000 processor on the same bus** could send the same keys. The two
  sources can still be told apart by CAN source address.
- **B&G could one day define keys 66–82 differently.** That's unlikely, because B&G's
  keys 0–255 already follow the Fastnet channel map.

## The frame, byte by byte

A raw heading reading of 28173, as it goes on the wire (source address 0x23 here; the
bridge uses whatever address it claimed):

```
CAN ID 1DFF0823   data 40 06 7D 99 4A 20 0D 6E
```

### CAN identifier: `1DFF0823`

29-bit extended ID:

| Bits | Field | Value |
|---|---|---|
| 26–28 | priority | 7 (lowest) |
| 8–25 | PGN | 0x1FF08 = 130824 |
| 0–7 | source address | 0x23 |

130824 is a PDU2 (broadcast) PGN, so bits 8–25 are the PGN as they stand; there is
no destination byte to mask off.

### Fast-packet framing: `40 06`

130824 is a fast-packet PGN. The first frame of every message starts with:

- **byte 0 = `40`:** sequence counter in the top 3 bits (here 2), frame index in the
  low 5 bits (0 = first frame). The counter advances by one per 130824 message
  (`00`, `20`, `40` … `E0`, then wraps), so a receiver can tell consecutive messages
  apart.
- **byte 1 = `06`:** total payload length in bytes.

The first frame carries up to 6 payload bytes. One raw value is exactly 6 bytes, so
**every raw message is a single CAN frame.**

### Payload: `7D 99 4A 20 0D 6E`

All fields are little-endian.

| Bytes | Field | Decoded |
|---|---|---|
| `7D 99` | header, 0x997D | manufacturer 381 (B&G), bits 0–10 · reserved `11`, bits 11–12 · industry 4 (marine), bits 13–15 |
| `4A 20` | key/length, 0x204A | key 0x04A = 74 (Heading Raw), bits 0–11 · length 2, bits 12–15 |
| `0D 6E` | value | 0x6E0D = 28173, signed 16-bit |

Real B&G frames start `7d 99` as well [1][4]: B&G sets the reserved bits.

### All four keys

Encoded by the bridge from values in the test captures:

| Channel | Value | Frame data |
|---|---|---|
| Boatspeed (Raw) | 492 | `00 06 7D 99 42 20 EC 01` |
| Heading (Raw) | 28173 | `40 06 7D 99 4A 20 0D 6E` |
| Apparent Wind Speed (Raw) | 778 | `80 06 7D 99 4E 20 0A 03` |
| Apparent Wind Angle (Raw) | -14969 | `C0 06 7D 99 52 20 87 C5` |

## The values

**Signed 16-bit, unscaled, exactly as Fastnet carries them.** All four raw channels
arrive in Fastnet format 0x0A, "pairedScaledInt": two signed 16-bit numbers [5]. The
bridge sends the first number unchanged.

What the numbers mean is not documented by B&G. From the captures:

- **Heading and wind angle look like binary angles** (65536 counts = 360°, signed, so
  ±180°). Raw heading tracks the calibrated heading at about 182 counts per degree:
  41° reads 7389, and 315° (-45°) reads -8246. The small differences from an exact
  binary angle are presumably the calibration being removed. Observed ranges:
  heading -8481 to 28264, wind angle -17509 to 26611.
- **The speeds are sensor counts** with no known fixed scale; converting them to a
  speed is what calibration does. Observed ranges: boatspeed 0 to 529, wind speed
  338 to 1906.

## Sending rules

- **One key per message.** A second key would make the payload 10 bytes and the
  message two CAN frames, so separate messages are the cheapest option.
- **Full rate.** Every update is sent, with no rate cap: the triggers are tagged
  `full_rate`, which exempts them from `MIN_SEND_INTERVAL`. Raw heading updates at
  about 25 Hz and the other three at about 16 Hz each, so together about 70 frames/s,
  roughly 4% of a 250 kbit/s bus. (The captures agree on the ratio: 1970 raw heading
  updates against about 1210 for each of the others.)
- **Priority 7**, the lowest. Proprietary PGNs have no standard priority, and the
  volume must never delay navigation data. `--n2k-priority` overrides it along with
  everything else.
- **No "not available" value.** When a channel has no value, no message is sent. A
  value that doesn't fit signed 16 bits isn't sent either, and a throttled warning is
  logged; it is never masked into a wrong number.
- **`--ignore-pgn 130824`** turns off all four.

## Decoding

This parses a CAN frame the way flightrecorder_n2k stores it (CAN ID plus data
bytes). It was checked against the frames above and against canboat's real B&G
sample.

```python
import struct

BANDG, MARINE = 381, 4
RAW_KEYS = {0x42: "stw_raw", 0x4A: "heading_raw", 0x4E: "aws_raw", 0x52: "awa_raw"}


def bandg_entries(payload: bytes):
    """A reassembled PGN 130824 payload -> [(key, value_bytes)], or [] if the
    payload isn't B&G's (Maretron and Mercury also use 130824)."""
    header, = struct.unpack_from("<H", payload, 0)
    if header & 0x7FF != BANDG or header >> 13 != MARINE:
        return []
    entries, i = [], 2
    while i + 2 <= len(payload):
        key_len, = struct.unpack_from("<H", payload, i)
        key, length = key_len & 0xFFF, key_len >> 12
        entries.append((key, payload[i + 2:i + 2 + length]))
        i += 2 + length
    return entries


def raw_channels(can_id: int, data: bytes):
    """One CAN frame -> {column: count} for the fastnet2n2k raw keys.
    fastnet2n2k's raw messages are always a single fast-packet frame."""
    if (can_id >> 8) & 0x3FFFF != 130824:     # PDU2, so the PGN is bits 8-25
        return {}
    if data[0] & 0x1F != 0:                   # not the first frame of a message
        return {}
    payload = data[2:2 + data[1]]             # data[1] = total payload length
    return {RAW_KEYS[key]: struct.unpack_from("<h", value)[0]
            for key, value in bandg_entries(payload)
            if key in RAW_KEYS and len(value) >= 2}
```

Things to know:

- **`raw_channels` handles single-frame messages only**, which is all the bridge
  sends. Real B&G 130824 messages usually carry many keys across several frames. To
  read those, reassemble the fast-packet first (by source address and sequence
  counter), then pass the payload to `bandg_entries`.
- **Read only the first two bytes of each value** (`unpack_from`). That keeps the
  decoder working if the second number of each Fastnet pair is ever added as a 4-byte
  value under the same key (see below).
- **Don't rely on the `nmea2000` library's decode of the value.** It returns each
  value's bytes in reverse order. Parse the payload directly, as above. The library's
  *encoder* is correct; the bridge uses it.
- **Generic tools won't name these keys.** canboat-based analysers decode the B&G
  header and entry structure, but keys 66, 74, 78 and 82 aren't in canboat's key
  table, so expect a bare number or "unknown" rather than a channel name.

## Not in it yet

- **The second value of each raw pair.** Fastnet format 0x0A carries two signed 16-bit
  numbers per raw channel, but pyfastnet passes on only the first, so only that is
  sent. The second varies: raw wind speed reads `778 / 701`, raw boatspeed
  `492 / 2092`, and raw heading usually repeats the first. What it means is unknown.
  Sending it needs a pyfastnet change. On the wire it would become a 4-byte value,
  length 4: the first number then the second, both signed 16-bit little-endian. That
  makes each message 8 bytes, which is two CAN frames.
- **The remaining performance channels.** VMG and next-tack heading now go out (see
  below), but the same PGN is also how B&G processors carry target TWA, polar
  performance, mast angle and more. The bridge decodes those and still doesn't send
  them. Adding them is the same three lines each, in the value types canboat
  documents [2] — with the signedness caveat below.
- **Recorder columns.** flightrecorder_n2k archives every CAN frame, so the raw frames
  are captured already. Turning them into columns needs a parser like the one above;
  `aws_raw`, `awa_raw` and `stw_raw` exist already (fed from NMEA 0183 XDR today),
  and raw heading would be new.

## History

The raw channels were first sent as single-frame PGNs 65280 (raw wind speed and
angle), 65281 (raw heading) and 65282 (raw boatspeed), byte-identical to
[fastnet2ip](https://github.com/ghotihook/fastnet2ip)'s. That version (commit
`9f3079e`) was reverted, and 130824 replaced it in 3.4.0. fastnet2ip moved to the same
130824 frames in its 3.3.0, so one decoder reads both bridges. The old format had
three defects:

- **Its header was `7D 81`**, with the reserved bits clear. Real B&G frames send
  `7D 99`.
- **It documented the values as `uint16`**, but raw heading and wind angle are signed.
  A decoder following the documentation misreads every negative angle, and -1 collided
  with its 0xFFFF "no data" value.
- **It needed a monkeypatch.** The `nmea2000` library has no encoder for those
  messages, so the reverted code injected one. The library has a native 130824
  encoder, so the current version needs none.

## References

1. canboat, PGN 130824 "B&G: key-value data": field layout and real B&G samples.
   <https://github.com/canboat/canboat/blob/master/database/pgns/130824-bGKeyValueData.yaml>
2. canboat, `BANDG_KEY_VALUE` lookup: B&G's key table, names and value types.
   <https://github.com/canboat/canboat/blob/master/database/lookups/BANDG_KEY_VALUE.yaml>
3. canboat discussion #354, "Supporting navico key-value pairs in pgns": how the
   format was worked out from captures.
   <https://github.com/canboat/canboat/discussions/354>
4. canboat sample captures from B&G systems: `bandg_tritonedge.raw`, `bandg_zeuss.raw`.
   <https://github.com/canboat/canboat/tree/master/samples>
5. pyfastnet, `fastnet_decoder/data/fastnet.json`: Fastnet format 0x0A
   ("pairedScaledInt") and the raw channel definitions.
   <https://github.com/ghotihook/pyfastnet>
6. Navico GoFree Tier 1 and Tier 2 WebSocket specifications, as bundled with the
   B&G H5000 Logger project: the public B&G/Navico interface, which has no raw items.
   <https://github.com/LoonSongSoftware/B-G-H5000-Logger>
7. `nmea2000` (tomer-w): the canboat-based library whose `encode_pgn_130824_bGKeyValueData`
   the bridge uses. <https://github.com/tomer-w/nmea2000>
8. canboat PGN database: definitions of the proprietary PGN ranges and the
   manufacturer code table (381 = B & G).
   <https://github.com/canboat/canboat/tree/master/database>


---

# Performance channels: VMG and next-tack heading

**Status: shipped in 3.5.0.** The code is `_BANDG_PERF_KEYS` / `_bandg_perf` in
[`fastnet2n2k/mapping.py`](../fastnet2n2k/mapping.py).

## What is sent

| Key | Fastnet channel | pyfastnet path | Value |
|---|---|---|---|
| 127 (0x7F) | Velocity Made Good (Knots) | `performance.velocityMadeGood` | 16-bit, 0.01 m/s |
| 154 (0x9A) | Heading on Next Tack | `performance.tackMagnetic` | 16-bit, 0.0001 rad, 0–2π |

Neither has a standard NMEA 2000 PGN. Unlike the raw channels above, these are keys
**real B&G gear does send**, in value types canboat documents [2] — canboat's own B&G
captures [4] contain both. So a B&G or Navico display on the bus can show them
natively, and nothing about the format is invented here.

As before, the key is simply the Fastnet channel number.

## The frame

A next-tack heading of 355°, and a VMG of 4.15 kn:

```
7d 99 9a 20 07 f2      key 154, length 2, value 61959  = 6.1959 rad = 355.0°
7d 99 7f 20 d5 00      key 127, length 2, value   213  = 2.13 m/s  = 4.15 kn
```

Same header and same key/length packing as the raw channels. Both values are 16-bit,
so each message is 6 payload bytes and therefore **a single CAN frame** — which also
keeps them from interleaving multi-frame messages with the full-rate raw stream on the
same PGN and source address.

**Priority 3**, not the raw channels' 7: that is what real B&G gear sends this PGN at
(all 19 of the 130824 messages in canboat's `bandg_tritonedge.raw` are priority 3).
They are rate-capped by `MIN_SEND_INTERVAL` like every other channel; only the raw
channels are exempt.

## ⚠️ canboat marks the angle keys signed. They are not.

canboat's `BANDG_KEY_VALUE` [2] gives key 154 — and every other angle key —
`Signed: true`. **Following that puts any heading above 180° out by 15.5°**, because a
signed reading of raw *r* is `r × 0.0001 − 6.5536` while the true value is
`r × 0.0001`, and 2π is 6.2832, not 6.5536.

The measurement. canboat's `bandg_tritonedge.raw` [4] carries both PGN 130824 and the
**standard** PGN 130306 from the same B&G box. Key 157 is "Wind Angle to Mast", which
130306's apparent wind angle should track:

```
10:39:46.213  key 157  raw=41816   unsigned=239.6°   signed=-135.9°
10:39:46.245  130306                        AWA=240.3°
10:39:46.458  key 157  raw=41756   unsigned=239.2°   signed=-136.2°
10:39:46.445  130306                        AWA=240.2°
10:39:46.710  key 157  raw=41835   unsigned=239.7°   signed=-135.8°
10:39:46.696  130306                        AWA=240.3°
```

Unsigned tracks the standard PGN to about a degree; signed is 375° away. The
resolution corroborates it: 2π / 0.0001 = 62832, which fits uint16 with room to spare
— 0.0001 rad is the coarsest step that packs a full circle into 16 bits, exactly as
the standard NMEA 2000 angle fields do it.

So these keys are encoded like any other N2K angle, and `mapping.py`'s existing
`_wrap()` applies unchanged. `tests/test_mapping.py::test_tack_heading_above_pi_stays_unsigned`
guards it.

This is worth a canboat issue; it has not been filed.

## VMG is a magnitude, not a signed quantity

VMG to windward is physically negative downwind — `boatspeed × cos(TWA)` with TWA past
90°. **The H2000 does not send it that way: it drops the sign.**

Measured on `tests/data/big_with_ap_actions.txt`, which is entirely downwind (TWA
111–150°, 67 co-observed samples):

| Hypothesis | mean error | stdev |
|---|---|---|
| VMG = \|boatspeed × cos(TWA)\| | −0.033 m/s | 0.050 |
| VMG = boatspeed × cos(TWA) | **+2.850 m/s** | 0.826 |

At TWA 118° with 2.46 m/s of boatspeed the signed quantity is −1.16 m/s and the
instrument puts **+1.13** on the wire.

So canboat's `Signed: false` on key 127 is **correct**, unlike its angle flags, and the
`max(0.0, …)` in `_bandg_perf` is a guard that has never fired on any capture rather
than a lossy choice. **For consumers: upwind or downwind is implied by the wind angle,
never by this key's sign.**

## Sending rules

- **One key per message**, single CAN frame, as above.
- **Rate-capped** at `MIN_SEND_INTERVAL` (0.05 s per path). These are display values;
  the full-rate exemption is only for the raw logging channels.
- **No "not available" value.** When a channel has no value, no message is sent. A
  value that doesn't fit its 16-bit key isn't sent either, and a throttled warning is
  logged.
- **`--ignore-pgn 130824`** turns these off along with the four raw channels.

## Verified

`tests/test_mapping.py` asserts the frames byte for byte, the unsigned angle encoding,
the VMG clamp, a canboat round-trip, and the rate cap. Replaying all eight bundled
captures produces **953 messages, no value out of range, every one a single CAN
frame** — raw 0–213 for key 127 and 349–62134 for key 154.
