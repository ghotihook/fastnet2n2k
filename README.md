# fastnet2n2k

Reads a **B&G Fastnet** instrument stream (live serial or a captured hex file),
decodes it with [pyfastnet](https://github.com/ghotihook/pyfastnet), maps the
channels to **NMEA 2000** PGNs and transmits them onto a **physical CAN bus** via
SocketCAN. Built for the
[M5Stack CoreMP135](https://docs.m5stack.com/en/core/M5CoreMP135) but runs on any
Linux box with a SocketCAN interface. Requires **Python 3.11+**.

## Quick start

**1. Install** the CLI globally, in its own isolated environment, with
[pipx](https://pipx.pypa.io/):

```bash
sudo apt install pipx                    # once, if you don't have it
sudo pipx install --global fastnet2n2k
```

This puts a `fastnet2n2k` command in `/usr/local/bin`, where a root-run systemd
service can find it too. (`--global` needs pipx ≥ 1.5.) To upgrade later, see
[Upgrading](#upgrading).

**2. Bring up the CAN bus** (once per boot — `restart-ms 100` lets the controller
auto-recover from a bus-off):

```bash
sudo ip link set can0 up type can bitrate 250000 restart-ms 100
ip -details link show can0    # want: state ERROR-ACTIVE, bitrate 250000
```

**3. Run it** — replaying a captured hex file is the safest first test:

```bash
# replay a captured Fastnet hex file
fastnet2n2k --file capture.txt --channel can0

# live from the Fastnet bus
fastnet2n2k --serial /dev/ttyUSB0 --channel can0
```

Find your serial adapter with `ls /dev/ttyUSB* /dev/ttyACM* /dev/ttyS*`. Add
`--live-data` to print the live channel table once per second. Stop with Ctrl-C.

> If `can0` shows `BUS-OFF`, fix that before expecting output — a CAN frame needs
> at least one other node on the wire to acknowledge it (check termination ≈ 60 Ω,
> common ground, and CAN-H/CAN-L not swapped). See
> [Verify](#verify) for a no-instruments loopback test.

<details>
<summary>Alternative: install from source (development)</summary>

```bash
git clone https://github.com/ghotihook/fastnet2n2k.git
cd fastnet2n2k
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```
</details>

### The Fastnet toolkit

Three projects stack together — pick the one that matches where you want the data
to end up:

| Project | What it does | Use it when |
|---|---|---|
| [pyfastnet](https://github.com/ghotihook/pyfastnet) | **Decoder library.** Turns raw Fastnet bytes into Signal K paths in SI units. | You're writing your own Python and want the decoded data. |
| [fastnet2ip](https://github.com/ghotihook/fastnet2ip) | **Serial → network.** Broadcasts decoded data over UDP as NMEA 0183 or NMEA 2000 (over IP). | Feeding Signal K, OpenCPN, or a plotter over WiFi / Ethernet. |
| **fastnet2n2k** *(this app)* | **Serial → physical NMEA 2000 bus.** Transmits PGNs onto a CAN backbone via SocketCAN. | Wiring into a real NMEA 2000 network / chartplotter. |

```
                          ┌─ fastnet2ip   → UDP (NMEA 0183 / NMEA 2000 over IP) → Signal K, OpenCPN, plotters
B&G Fastnet bus ─(serial)─→ pyfastnet ─┤
                          └─ fastnet2n2k → SocketCAN (NMEA 2000 PGNs)           → CAN backbone, chartplotter
```

This app builds on `pyfastnet` and puts decoded data onto a **physical** NMEA 2000
CAN backbone. If instead you want it on your **network** (UDP — Signal K, OpenCPN,
a plotter over WiFi), use [fastnet2ip](https://github.com/ghotihook/fastnet2ip).

## Running as a systemd service

For an always-on bridge, run `fastnet2n2k` under systemd so it starts on boot and
restarts on failure. The unit runs as **root** (consistent with
[fastnet2ip](https://github.com/ghotihook/fastnet2ip)) and brings `can0` up itself
before starting.

> **Install it globally, not per-user.** A plain `pipx install` goes to a user's
> `~/.local/bin`, which a root-run service can't rely on. Use `pipx install
> --global` so the command lands in `/usr/local/bin` instead.

**1. Install globally with pipx** as in [Quick start](#quick-start) step 1.

This gives you `/usr/local/bin/fastnet2n2k`, the path the unit below uses
(run `which fastnet2n2k` to confirm it).

**2. Create the unit file**

A template ships as [`fastnet2n2k.service`](fastnet2n2k.service) in the source
repo. Copy it to `/etc/systemd/system/` and edit the `--serial` device and channel
to match your setup:

```ini
[Unit]
Description=fastnet2n2k Service
After=network.target

[Service]
Type=simple
User=root

# Bring can0 up at the NMEA2000 bitrate — only if it isn't already up, so this is
# safe alongside another CAN service (e.g. an n2k2ip gateway) sharing can0.
ExecStartPre=/bin/sh -c 'ip link show can0 | grep -qw UP || ip link set can0 up type can bitrate 250000 restart-ms 100 2>/dev/null; ip link show can0 | grep -qw UP'

ExecStart=/usr/local/bin/fastnet2n2k --serial /dev/ttyAMA5 --channel can0
Restart=always
RestartSec=10

# === RESOURCE LIMITS ===
OOMScoreAdjust=-700
OOMPolicy=continue
MemoryMax=128M
MemoryHigh=96M
TimeoutStopSec=30

# === LOGGING ===
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fastnet2n2k
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

**3. Enable and start it**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fastnet2n2k.service
journalctl -u fastnet2n2k.service -f      # follow the logs
```

To upgrade later, see [Upgrading](#upgrading).

## Upgrading

To move to the latest release, upgrade and restart the service:

```bash
sudo pipx upgrade --global fastnet2n2k
sudo systemctl restart fastnet2n2k
```

Check which version you're on with `sudo pipx list --global --short` (prints e.g.
`fastnet2n2k 3.5.0`). Releases are listed in the
[release history on PyPI](https://pypi.org/project/fastnet2n2k/#history).

## Command-line options

| Option | Default | Meaning |
|---|---|---|
| `--serial DEV` / `--file PATH` | — | input source (one is required) |
| `--channel` | `can0` | SocketCAN interface |
| `--n2k-priority` | per-PGN standard | override CAN priority (0–7, 0 = highest) for **all** transmitted frames; if omitted, each PGN keeps its standard priority (see the PGN table below) |
| `--ignore-pgn PGN` | nothing suppressed | don't transmit this PGN (and don't advertise it); repeatable and/or comma-separated — see below |
| `--unique` | from hostname | device NAME unique number (so two boards don't claim the same NMEA 2000 NAME) |
| `--live-data` | off | print the live channel table to the console once per second |
| `--log-level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (`DEBUG` also turns on pyfastnet's per-frame decode logging) |

### Suppressing PGNs

If another device on the bus is the authority for some data — a GPS for position and
COG/SOG, say — stop this bridge sending its version of it:

```bash
fastnet2n2k --serial /dev/ttyUSB0 --ignore-pgn 129025,129026
fastnet2n2k --serial /dev/ttyUSB0 --ignore-pgn 129025 --ignore-pgn 129026
```

A suppressed PGN is never built and never sent, and is also dropped from the
transmit-PGN list this node advertises to the bus, so it doesn't claim to send what
it won't. Only PGNs from the table below are accepted; anything else is a startup
error rather than a silent no-op, so a typo can't look like it worked.

Suppression is per **PGN**, and some PGNs carry more than one kind of data:
`--ignore-pgn 130306` silences apparent wind, true wind *and* TWD,
`--ignore-pgn 130312` silences both sea *and* air temperature, and
`--ignore-pgn 130824` silences the four raw sensor channels *and* VMG and next-tack
heading.

The source address is **not** a flag — it is left to the `nmea2000` library, which
picks a preferred address and resolves conflicts via ISO address claiming, then
persists the result across restarts.

**CAN failure handling:** the device reconnects automatically. If `can0` isn't up
at start it waits (logging retries) rather than exiting; if the bus drops or goes
bus-off mid-run, sends fail quietly (logged at most every 5 s) and resume once it
recovers — the bridge keeps running. Use `--log-level DEBUG` for connection/retry
detail.

## What it sends

Each Fastnet channel is mapped to the matching PGN and emitted **only when the
channel updates**, rate-capped at 0.05 s per path (the raw sensor channels are the
exception: they go out at full rate). There is no periodic re-broadcast, so when the
instruments go quiet the output stops and consumers time the data out themselves.

| Data | PGN | Priority | Notes |
|---|---|---|---|
| Heading | 127250 | 2 | Magnetic or True per the instrument |
| Apparent / True wind, TWD | 130306 | 2 | reference per the instrument |
| Boat speed | 128259 | 2 | |
| Depth | 128267 | 3 | value is below **keel**; offset field sent as not-available — see note below |
| COG/SOG | 129026 | 2 | prefers True COG, falls back to Magnetic |
| Attitude (heel/trim) | 127257 | 3 | |
| Rudder, Leeway, Rate of turn | 127245 / 128000 / 127251 | 2 / 4 / 2 | |
| Distance log, XTE | 128275 / 129283 | 6 / 3 | |
| Position | 129025 | 2 | |
| Sea / air temperature | 130312 | 5 | |
| Barometric pressure | 130314 | 5 | |
| Tidal set & drift | 129291 | 3 | reference per the instrument |
| House battery voltage | 127508 | 6 | instance 0 |
| Autopilot mode & target | 127237 | 2 | see note below |
| VMG, next-tack heading | 130824 | 3 | B&G key-value data — see note below |
| Raw boatspeed, heading, AWS, AWA | 130824 | 7 | B&G key-value data, full rate — see note below |

The **Priority** column is each PGN's NMEA 2000 standard CAN priority (0 = highest,
7 = lowest) — the values used unless you override them all with `--n2k-priority N`.
130824 is proprietary and has no standard priority: VMG and next-tack heading use 3,
which is what real B&G gear sends it at, and the raw channels use the lowest, so their
volume never delays navigation data.

Data arrives from pyfastnet already in **SI** on Signal K paths, so it maps almost 1:1
onto NMEA 2000 — no unit conversion here (the raw sensor channels are sensor counts,
and pass through as counts). **Sign** comes straight from the decoded value; **True
vs Magnetic** is carried by the path (the B&G instrument's own reference — the Fastnet
stream has no variation to convert). Encoding, CAN-ID construction, fast-packet
framing and ISO address claiming (250 kbit/s, 29-bit IDs) are handled by the
[`nmea2000`](https://github.com/tomer-w/nmea2000) library (canboat-based) on top of
`python-can`'s socketcan backend.

> **Depth is below keel.** The B&G/H2000 applies its keel offset internally, so the
> depth on the Fastnet wire is already **below-keel**. Fastnet never reports the
> transducer-to-keel distance, so PGN 128267's **offset field is sent as
> not-available** — the honest encoding for "no offset info" (sending `0` would
> assert a transducer-at-keel distance we don't actually know). The depth value
> passes through unchanged either way.
>
> Two consequences for consumers:
> - The reading appears under Signal K `environment.depth.belowTransducer`, because
>   PGN 128267's depth field *is* below-transducer by definition and `belowKeel` is a
>   derived path. The **number is your below-keel depth**; only the label differs. To
>   get a `belowKeel` path, set `transducerToKeel = 0` on the Signal K side and enable
>   the derived-data plugin.
> - **Do not** configure a second keel/transducer offset on any downstream plotter or
>   gateway — it would double-count and read shallow.

> **Autopilot (127237 Heading/Track Control).** The H2000 pilot's mode is sent as the
> PGN's steering mode, with the compass target as heading-to-steer:
>
> | Pilot mode | Steering mode | Heading-to-steer |
> |---|---|---|
> | Standby | Main Steering | not available |
> | Compass | Heading Control | compass target |
> | Wind | Heading Control | compass target |
> | Power | Non-Follow-Up Device | not available |
> | NMEA WP | Track Control | compass target |
>
> - **Wind and compass look the same on the bus** — the standard has no wind mode.
> - **The target is dropped in standby and Power** even though Fastnet keeps sending
>   the last one, so a disengaged pilot never advertises a heading it isn't steering to.
> - **Engaging reads 0° for one update** before the real target arrives. It passes
>   through, because a 0° target can't be told apart from north.
> - It is a status broadcast only. A plotter may show autopilot controls on seeing it;
>   they do nothing, since nothing here listens for commands. If that is unwanted, or
>   another autopilot on the NMEA 2000 bus already sends 127237, use `--ignore-pgn 127237`.

> **B&G key-value data (130824).** Six channels have no standard NMEA 2000 PGN, so
> they go out in B&G's own proprietary key-value PGN, in B&G's layout: the B&G
> manufacturer header (`7D 99`: code 381, marine), then a 12-bit key and 4-bit byte
> length, then the value. B&G's keys are **Fastnet channel numbers** (canboat's
> `BANDG_KEY_VALUE` table: 65 = Water Speed = 0x41, …), so each channel keeps its own.
> Every message carries one key and fits a single CAN frame. Displays ignore keys they
> don't know, and `--ignore-pgn 130824` turns off all six.
>
> | Key | Fastnet channel | Value | Priority | Rate | For |
> |---|---|---|---|---|---|
> | 127 (0x7F) | Velocity Made Good | unsigned 16-bit, 0.01 m/s | 3 | capped | display |
> | 154 (0x9A) | Heading on Next Tack | unsigned 16-bit, 0.0001 rad, 0–2π | 3 | capped | display |
> | 66 (0x42) | Boatspeed (Raw) | signed 16-bit, unscaled | 7 | full | logging |
> | 74 (0x4A) | Heading (Raw) | signed 16-bit, unscaled | 7 | full | logging |
> | 78 (0x4E) | Apparent Wind Speed (Raw) | signed 16-bit, unscaled | 7 | full | logging |
> | 82 (0x52) | Apparent Wind Angle (Raw) | signed 16-bit, unscaled | 7 | full | logging |
>
> **Performance channels (VMG, next-tack heading)** use keys real B&G gear itself
> sends, so a B&G or Navico display on the bus can show them natively.
> - **Angles are unsigned 0–2π**, like a standard NMEA 2000 angle field. canboat's key
>   table marks them signed, which is wrong — see the doc for the measurement. Getting
>   this backwards puts any heading above 180° out by 15.5°.
> - **VMG is a magnitude.** The H2000 drops the sign, so downwind VMG arrives positive;
>   upwind or downwind is implied by the wind angle, never by this value.
>
> **Raw sensor channels** are the uncalibrated readings, sent so calibration can be
> refitted later from logged data. They are not for display.
> - **Values are exactly as Fastnet carries them** (format 0x0A). Heading and wind
>   angle are binary angles (65536 = 360°); the speeds are sensor counts. B&G gear has
>   never been seen sending these four keys, so this value format is ours.
> - **Every update is sent**, at whatever rate the instruments produce it — about
>   70 frames/s for all four, ~4% of the bus.
> - Fastnet actually carries **two** 16-bit values per raw channel; pyfastnet exposes
>   only the first, so only that is sent. The second could follow later as a 4-byte
>   value under the same key without breaking decoders that honour the length.
>
> The frames byte by byte, why they use B&G's format, the measurements behind the
> performance-channel encoding, a decoder and references are in
> [`docs/bandg_130824_raw_channels.md`](docs/bandg_130824_raw_channels.md).

> **WiFi gateways:** if you feed a WiFi NMEA 2000 gateway downstream, configure it
> for **unicast** UDP, not broadcast — WiFi broadcast is unacknowledged and
> silently drops frames even at low rates.

## Verify

Watch the raw frames on the board with `can-utils` (`sudo apt install can-utils`):

```bash
candump -ta can0
```

You should see 29-bit frames appear as instruments update — heading, wind, depth,
speed, etc. — and stop when they go quiet. On a connected chartplotter / analyzer
the device appears in the device list after its ISO address claim (PGN 60928).

### Loopback test (no instruments needed)

With `can0` and `can1` wired together (CAN-H↔CAN-H, CAN-L↔CAN-L, one 120 Ω
terminator), `can1` provides the ACK so `can0` can transmit:

```bash
sudo ip link set can1 up type can bitrate 250000 restart-ms 100
candump -ta can1                                       # terminal 1
fastnet2n2k --file capture.txt --channel can0          # terminal 2
```

## Hardware notes

### M5Stack CoreMP135

The CoreMP135 runs Linux on an STM32MP135 and exposes its two FDCAN interfaces
(SIT1051T transceivers) as the SocketCAN netdevs `can0` (FDCAN1, PE3/PE10) and
`can1` (FDCAN2, PG0/PE0). Bring up the second interface the same way if you need it:

```bash
sudo ip link set can1 up type can bitrate 250000 restart-ms 100   # FDCAN2
```

### Raspberry Pi built-in UART (`/dev/ttyAMA*`)

On a Pi (including a CM4, which the sample service file targets with `/dev/ttyAMA5`),
the first open of a built-in PL011 UART after a boot can leave the hardware at 9600
even though 28800 was requested — Fastnet's rate is non-standard, so it takes the
kernel's `BOTHER` path, which has a known first-open quirk. `fastnet2n2k` works around
it automatically (`_force_baudrate` in `input_source.py`); the full diagnosis and the
supporting measurements are in [`docs/uart_first_open_baud_fix.md`](docs/uart_first_open_baud_fix.md).

### RS485 tap boards with automatic direction control

If the Fastnet side is tapped through an RS485 transceiver with **automatic direction
control** (Waveshare's carrier boards and HATs use one: TX low raises DE, so the driver
turns on whenever TX is low), check what the TX pin does *during boot*, before the UART
overlay muxes it.

On BCM2711 the pad reset defaults split at GPIO8 — **GPIO0-8 pull up, GPIO9-27 pull
down** — so a TX pin in the pull-down range sits low from reset until the kernel claims
it, holding the driver on and **jamming the instrument bus for the length of every
boot**. The bridge never transmits, so this is invisible from the software side: the
damage is done before its process starts, and by the time data is flowing the bus is
healthy again with the instrument alarm already latched.

Pin the line high from the firmware, using the TX GPIO of whichever channel you are on:

```
# /boot/firmware/config.txt
gpio=12=op,dh
```

Full diagnosis, the channel/overlay mapping for a CM4 in a CM5 carrier, and how to
re-confirm it on the wire are in
[`docs/rs485_tx_boot_glitch.md`](docs/rs485_tx_boot_glitch.md).

## Tests

```bash
source .venv/bin/activate
pip install -e . pytest
python -m pytest tests/ -q
```

The suite drives the mapping with the Fastnet captures in `tests/data/` and
round-trips the resulting NMEA 2000 messages to assert PGNs, units, T/M references,
sign passthrough, the autopilot mode sequence, the B&G 130824 byte layout for the raw
and performance channels (including the unsigned angle encoding), the send throttle
(and the raw channels' exemption from it), and the full file→decode→send pipeline.

## Sender POC (`nmea2000_poc.py`)

A standalone minimal proof-of-concept (in the source repo) that transmits a single
NMEA 2000 PGN (127250 Vessel Heading) onto the bus — useful for smoke-testing a
CAN link independently of the Fastnet pipeline.

```bash
python nmea2000_poc.py --channel can0 --heading 90 --once   # single frame
python nmea2000_poc.py --channel can0 --heading 90          # ~10 Hz loop
```

Options: `--channel` (default `can0`), `--heading` degrees, `--ref true|magnetic`,
`--rate` Hz, `--once`.

### Desk testing without a bus (virtual CAN)

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
candump vcan0                                                 # terminal 1
python nmea2000_poc.py --channel vcan0 --heading 90 --once   # terminal 2
```

## License

MIT — see [LICENSE](LICENSE).
