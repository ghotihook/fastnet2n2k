# TODO

## Deferred: B&G proprietary raw PGNs 65280 / 65281 / 65282

Emit the raw (unfiltered) instrument channels as B&G manufacturer-proprietary
single-frame PGNs, so consumers get the raw sensor values alongside the standard
processed PGNs.

| PGN | Channels | Payload after the mfr header |
|---|---|---|
| 65280 | Apparent Wind Speed (Raw) + Apparent Wind Angle (Raw) | `<HH>` wsRaw, waRaw |
| 65281 | Heading (Raw) | `<H>` headingRaw |
| 65282 | Boatspeed (Raw) | `<H>` boatspeedRaw |

These are wired into `_CHANNEL_MAP` in `fastnet2n2k/mapping.py` as `"TODO: …
(deferred)"` sentinels today (so they decode but don't transmit).

### Wire format (must stay byte-identical to fastnet2ip)

- 2-byte manufacturer header `7D 81` = `struct.pack('<H', (4 << 13) | 381)`.
- Then raw `uint16` value(s), little-endian, **no scaling** — the pyfastnet
  "(Raw)" channels are already integer counts; pass them straight through.
- `0xFFFF` means "no data" (use it when a channel is absent).

This matches `fastnet2ip`'s `_n2k_proprietary` encoder and the `flightrecorder_n2k`
decoder (`_decode_proprietary`), so one decoder handles frames from either bridge.
**Verify byte-for-byte against fastnet2ip if you re-add this.**

### A working implementation already exists

Commit **`9f3079e`** ("Add B&G proprietary raw PGNs …") implemented all of this
with tests. It was reverted because (a) the integration is ugly and (b) the rate
is high (see below). Start from `git show 9f3079e` rather than from scratch.

### Why it's deferred

1. **Rate.** The raw channels are the fastest on the bus — Heading (Raw) ~25 Hz,
   raw wind / boatspeed ~16 Hz — so transmitting them adds a lot of traffic. Sort
   out the output-rate strategy first (dedupe / lower `MIN_SEND_INTERVAL` /
   decimate the raw PGNs). Note 65280 is a *combined* frame: trigger it from **one**
   channel and mark the other a sentinel, or it double-fires (one frame per channel).
2. **Integration is ugly.** The `nmea2000` encoder dispatches on
   `encode_pgn_<PGN>` existing in `nmea2000.pgns` and has **no extension API**, so
   the only hook into the correct send pipeline (which gives source-address
   substitution + python-can retry/reconnect for free — don't bypass it) is to
   register encoders on that module. The reverted version did this with an
   import-time `setattr` monkeypatch + a passthrough that returned bytes stashed on
   `raw_can_data`.

### If re-adding, do it cleanly

Isolate the whole adapter in its own `fastnet2n2k/proprietary.py`: own the frame
layout (header, packing, priority) in one place, expose `build_*` handlers and a
single explicit `register()` call (invoked from `__main__`, not as an import side
effect), so `mapping.py` stays declarative. Remember to add the PGNs back to
`TX_PGNS`.

## Deferred: output rate / cadence

Works, but the bus rate is higher than this setup needs. Today every Fastnet
update is transmitted, debounced only by `MIN_SEND_INTERVAL = 0.05 s`
(20 Hz/channel) with no dedupe (commit `6078efc`). Most channels are input-limited
at ~3.5–9 Hz (~70 frames/s total) while the only consumer that matters
(flightrecorder_n2k) buckets at **1 Hz with no carry-forward**, so it just needs
~1 message/channel/second.

Cheap, safe wins when revisited:
- **Temperature double-fire.** `°C` and `°F` are the same reading in two units and
  the instruments emit both, so PGN 130312 goes out twice. Make the `°F` channels
  sentinels (`"duplicate of … (°C)"`) like Depth already does for feet/fathoms.
  Halves 130312. (`process_sea_temp`/`process_air_temp` already prefer °C.)
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
