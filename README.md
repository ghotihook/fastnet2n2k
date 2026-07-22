# fastnet2n2k

Reads a **B&G Fastnet** instrument stream (live serial or a captured hex file),
decodes it with [pyfastnet](https://github.com/ghotihook/pyfastnet), maps the
channels to **NMEA 2000** PGNs and transmits them onto a **physical CAN bus** via
SocketCAN. Built for the
[M5Stack CoreMP135](https://docs.m5stack.com/en/core/M5CoreMP135) but runs on any
Linux box with a SocketCAN interface. Requires **Python 3.10+**.

NMEA 2000 runs at **250 kbit/s** with 29-bit extended IDs. Message encoding,
CAN-ID construction, fast-packet framing and ISO address claiming are handled by
the [`nmea2000`](https://github.com/tomer-w/nmea2000) library (canboat-based) on
top of `python-can`'s socketcan backend.

## Quick start

**1. Install** the CLI in its own isolated environment with
[pipx](https://pipx.pypa.io/):

```bash
sudo pipx install --global fastnet2n2k
```

This puts a `fastnet2n2k` command on your PATH (in `/usr/local/bin`, so a
root-run systemd service can find it too). `python -m fastnet2n2k ...` also works
once installed. (`--global` needs pipx ≥ 1.5.)

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
| [pyfastnet](https://github.com/ghotihook/pyfastnet) | **Decoder library.** Turns raw Fastnet bytes into named instrument channels. | You're writing your own Python and want the decoded data. |
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

**1. Install globally with pipx**

```bash
sudo apt install pipx                        # once, if you don't have it
sudo pipx install --global fastnet2n2k
```

This gives you `/usr/local/bin/fastnet2n2k`, the path the unit below uses.
(`--global` needs pipx ≥ 1.5; run `which fastnet2n2k` to confirm the path.)

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

ExecStart=/usr/local/bin/fastnet2n2k --serial /dev/ttyUSB0 --channel can0
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

To upgrade later:
`sudo pipx upgrade --global fastnet2n2k && sudo systemctl restart fastnet2n2k`.

## Command-line options

| Option | Default | Meaning |
|---|---|---|
| `--serial DEV` / `--file PATH` | — | input source (one is required) |
| `--channel` | `can0` | SocketCAN interface |
| `--n2k-priority` | per-PGN standard | override CAN priority (0–7, 0 = highest) for **all** transmitted frames; if omitted, each PGN keeps its standard priority (see the PGN table below) |
| `--unique` | from hostname | device NAME unique number (so two boards don't claim the same NMEA 2000 NAME) |
| `--live-data` | off | print the live channel table to the console once per second |
| `--log-level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

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
channel updates** (0.05 s minimum interval, 5 s maximum re-broadcast), so when the
instruments go quiet the output stops and consumers time the data out.

| Data | PGN | Priority | Notes |
|---|---|---|---|
| Heading | 127250 | 2 | T/M reference taken from the Fastnet display layout (`°M`/`°T`) |
| Apparent / True wind, TWD | 130306 | 2 | knots→m/s, deg→rad; reference per layout |
| Boat speed | 128259 | 2 | knots→m/s |
| Depth | 128267 | 3 | metres |
| COG/SOG | 129026 | 2 | prefers True COG, falls back to Magnetic |
| Attitude (heel/trim) | 127257 | 3 | signed value passed through unchanged |
| Rudder, Leeway, Rate of turn | 127245 / 128000 / 127251 | 2 / 4 / 2 | |
| Distance log, XTE | 128275 / 129283 | 6 / 3 | NM→m |
| Position | 129025 | 2 | |
| Sea / air temperature | 130312 | 5 | °C/°F→Kelvin |
| Barometric pressure | 130314 | 5 | mbar→Pa |
| Tidal set & drift | 129291 | 3 | set deg→rad, drift kn→m/s; reference per layout |

The **Priority** column is each PGN's NMEA 2000 standard CAN priority (0 = highest,
7 = lowest) — the values used unless you override them all with `--n2k-priority N`.

**Units** are converted to NMEA 2000 SI. **Sign** comes straight from pyfastnet's
decoded value. **True/Magnetic** is read from the pyfastnet `layout` field (the
only place it exists); a bearing whose layout can't be resolved is skipped, never
guessed. The B&G proprietary raw PGNs (65280–65282) are deferred
(manufacturer-specific layout).

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

On the CoreMP135, the two FDCAN interfaces (SIT1051T transceivers) are exposed as
`can0` (FDCAN1, PE3/PE10) and `can1` (FDCAN2, PG0/PE0).

## Hardware notes (M5Stack CoreMP135)

The CoreMP135 runs Linux on an STM32MP135 and exposes its two FDCAN interfaces as
the SocketCAN netdevs `can0` and `can1`. Bring up the second interface the same
way if you need it:

```bash
sudo ip link set can1 up type can bitrate 250000 restart-ms 100   # FDCAN2
```

## Tests

```bash
source .venv/bin/activate
pip install -e ".[test]"   # or: pip install pytest
python -m pytest tests/ -q
```

The suite drives the mapping with pyfastnet's bundled capture files and
round-trips the resulting NMEA 2000 messages to assert PGNs, unit conversions,
T/M references, sign passthrough, the send throttle, and the full
file→decode→send pipeline.

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
