# TODO

## Blocked on a capture: waypoint navigation PGN 129284 (+ 129301)

pyfastnet decodes active-leg waypoint data that we don't transmit at all. Confirmed
present on the boat (live table, 2026-07-25, pyfastnet 3.1.0) — earlier work assumed
it was unavailable because **none of the captures in `tests/data/` contain any
waypoint paths** (they were all recorded with no active nav leg). That is the only
thing blocking this.

### 129284 Navigation Data — field → source path

| Field | Source path | Seen live? |
|---|---|---|
| `distanceToWaypoint` | `navigation.courseGreatCircle.nextPoint.distance` | yes (2963.2 m) |
| `bearingPositionToDestinationWaypoint` | `nextPoint.bearingTrue` / `bearingMagnetic` | yes (5.4454 rad) |
| `courseBearingReference` | from which bearing path is present | yes (True) |
| `bearingOriginToDestinationWaypoint` | `courseGreatCircle.bearingTrackTrue` / `bearingTrackMagnetic` | not yet |
| `etaTime`, `etaDate` | derive from `nextPoint.timeToGo` | not yet |
| `waypointClosingVelocity` | `nextPoint.velocityMadeGood` | not yet |
| origin/destination WP numbers, destination lat/lon | — | **never**: Fastnet has no waypoint identity |

The "not yet" paths exist in the decoder but weren't on the wire in that snapshot
(nav-mode / active-page dependent). Send them as not-available when absent, like the
existing triggers do — no special casing needed.

**Trap:** `performance.velocityMadeGood` is VMG *to windward*, a different quantity
from `waypointClosingVelocity`. The only correct source is
`courseGreatCircle.nextPoint.velocityMadeGood`. Don't substitute one for the other.

### What's out of reach, and what that costs

`129285` (Route/WP Information), `129302` (Bearing/Distance between Marks) and the
`130064`–`130074` Route & WP Service family all need waypoint names/IDs or a waypoint
database, which Fastnet never provides. That matters for consumers: Yacht Devices
document that `129284` carries only numeric waypoint identifiers, and that gateways
need `129285` alongside it to synthesise NMEA 0183 RMB/APB. So expect distance and
bearing to be usable while some MFDs decline to populate a "navigate to" page.
`129283` XTE is already transmitted and rides alongside.

`129301` (Time to+from Mark) needs `nextPoint.timeToGo`; its `markId` would be
not-available for the same reason. Cheap to add once that path is observed.

### Next step

Capture Fastnet hex **while navigating to a mark**, nav page up so `timeToGo` and
`bearingTrack*` are in the stream, into `tests/data/`. Then add the trigger(s) tagged
`@emits(129284)` and assert against the capture. Writing it before that means
shipping a PGN nothing has ever verified.

## Follow-ups to the raw channels in B&G 130824

The four raw channels go out as B&G key-value data (PGN 130824, keys = Fastnet
channel numbers) at full rate — see the README. This replaced the earlier 65280–65282
design (reverted commit `9f3079e`, and fastnet2ip's encoder), which used a malformed
header (`7D 81`: reserved bits clear, where real B&G frames send `7D 99`), documented
signed angles as `uint16`, and needed a monkeypatch that the library's native 130824
encoder makes unnecessary.

- **The second value of each raw pair.** Fastnet carries raw channels as format 0x0A,
  *two* signed 16-bit values (`display_text` shows `first / second`: AWS `778 / 701`,
  boatspeed `492 / 2092`, heading usually repeating the first). pyfastnet projects
  only `first`, so only that is sent. What `second` means is unknown. Exposing it
  needs a pyfastnet change; on the wire it becomes a 4-byte value (length 4) under
  the same key, which costs a second CAN frame per update.
- **Recorder decoding.** flightrecorder_n2k archives every CAN frame, so the raw
  values are captured already, but it needs a parser to turn them into columns
  (`aws_raw` / `awa_raw` / `stw_raw`, which the 0183 XDR path fills today, plus a new
  raw heading). Parse the payload directly: the `nmea2000` library's 130824 decode
  returns each value's bytes reversed.
- **Performance channels.** The same PGN is how B&G gear itself carries target TWA,
  polar performance, VMG, mast angle and the rest — all channels we decode but don't
  send. Sending them under B&G's own keys, in the value types canboat documents for
  those keys, would let a B&G/Navico display on the bus show them natively.

## Deferred: output rate / cadence

Works, but the bus rate is higher than this setup needs. Today every Fastnet
update is transmitted, debounced only by `MIN_SEND_INTERVAL = 0.05 s`
(20 Hz/channel) with no dedupe (commit `6078efc`). Most channels are input-limited
at ~3.5–9 Hz (~70 frames/s total) while the only consumer that matters
(flightrecorder_n2k) buckets at **1 Hz with no carry-forward**, so it just needs
~1 message/channel/second.

Out of scope: the raw sensor channels (130824) are deliberately exempt from the cap
(`full_rate`) and add ~57 frames/s on top — full rate is the point of them. Keep
them exempt if the cap is lowered.

Cheap, safe wins when revisited:
- **Temperature double-fire.** `°C` and `°F` are the same reading in two units and
  the instruments emit both, so PGN 130312 can go out twice. **Re-measure before
  acting** — pyfastnet v3 marks the `°F` channels `collapsed`, projecting them onto
  the *same* Signal K path as `°C` (as it does for depth in feet/fathoms). There is
  therefore no separate `°F` path left to exclude, and the fix this item originally
  proposed no longer applies. Any remaining double-fire would come from the one path
  updating twice per cycle, which `MIN_SEND_INTERVAL` already caps.
- **Lower the cap.** `MIN_SEND_INTERVAL` 0.05 → ~0.25 s (4 Hz). That's a 4× margin
  over the 1 Hz sink, keeps every bucket filled (no gaps), and thins the bus with
  one number. **Prefer the cap to dedupe** — dedupe (send-on-change) would punch
  NULL holes in the no-carry-forward sink for steady values (depth at anchor, temp).
- **Distance Log (128275)** looks like ~19 Hz but is fast-packet (3 CAN frames per
  message ≈ 6.4 msg/s, same as Depth). Not a bug; drops out of the global cap.

## Strategic: unify via Signal K

Longer term, replace this bespoke Fastnet→NMEA 2000 mapping with a **Signal K**-based
path so the whole boat data pipeline is unified (Signal K owns the N2K output, dedupe,
and cadence). When that lands, this custom `mapping.py` / cadence logic — and the
rate work above — goes away rather than being polished here.

## Investigate: dedicated can0.service vs inline ExecStartPre

Today both fastnet2n2k and n2k2ip bring can0 up with an idempotent `ExecStartPre`
one-liner (only configures the link if it isn't already up), which is safe to run
with multiple CAN services sharing can0 on one host. Works fine as-is.

Cleaner alternative for a multi-service box: a single oneshot `can0.service`
(`Type=oneshot`, `RemainAfterExit=yes`) that owns the bitrate / `restart-ms`
config, with each bridge dropping its `ExecStartPre` and declaring
`Requires=can0.service` + `After=can0.service`. Benefits: one source of truth for
the CAN config (no duplicated shell across units that can drift), real dependency
modelling, and the start-up race goes away structurally instead of being swallowed.

Wrinkle: can0 setup is a machine-level concern, not owned by either PyPI package —
so keep the inline `ExecStartPre` as the standalone default and ship `can0.service`
as an *optional* template for multi-service hosts. The two are compatible: with
`can0.service` present, the inline `ExecStartPre` just sees can0 already up and
no-ops.

Leave as-is for now; revisit if the multi-service setup grows.
