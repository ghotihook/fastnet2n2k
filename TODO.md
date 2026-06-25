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
