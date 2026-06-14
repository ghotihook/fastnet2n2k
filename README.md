# fastnet2n2k — B&G Fastnet → NMEA2000 bridge

Reads a **B&G Fastnet** instrument stream (live serial or a captured hex file),
decodes it with [`pyfastnet`](https://github.com/ghotihook/pyfastnet), maps the
channels to **NMEA2000** PGNs and transmits them onto a CAN bus. Built for the
[M5Stack CoreMP135](https://docs.m5stack.com/en/core/M5CoreMP135) but runs on any
Linux box with a SocketCAN interface.

The CoreMP135 runs Linux on an STM32MP135 and exposes its two FDCAN interfaces
(SIT1051T transceivers) as the SocketCAN netdevs `can0` (FDCAN1, PE3/PE10) and
`can1` (FDCAN2, PG0/PE0). NMEA2000 runs at **250 kbit/s** with 29-bit extended IDs.
Message encoding, CAN-ID construction, fast-packet framing and ISO address claiming
are handled by the [`nmea2000`](https://github.com/tomer-w/nmea2000) library (canboat-
based) on top of `python-can`'s socketcan backend.

## What it sends

Each Fastnet channel is mapped to the matching PGN and emitted **only when the
channel updates** (with a 0.05 s minimum interval and a 5 s maximum re-broadcast),
so when the instruments go quiet the output stops and consumers time the data out.

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

The **Priority** column lists each PGN's NMEA2000 standard CAN priority (0 = highest,
7 = lowest). These are the values used **by default** — when `--n2k-priority` is *not*
given, every frame keeps the per-PGN priority shown above. Passing `--n2k-priority N`
overrides the whole column, forcing all frames to a single priority `N`.

**Units** are converted to NMEA2000 SI. **Sign** comes straight from pyfastnet's
decoded value. **True/Magnetic** is read from the pyfastnet `layout` field (the only
place it exists); a bearing whose layout can't be resolved is skipped, never guessed.
The B&G proprietary raw PGNs (65280–65282) are deferred (manufacturer-specific layout).

> **WiFi gateways:** if you feed a WiFi NMEA2000 gateway downstream, configure it for
> **unicast** UDP, not broadcast — WiFi broadcast is unacknowledged and silently drops
> frames even at low rates.

## Install

```bash
git clone https://github.com/ghotihook/fastnet2n2k.git
cd fastnet2n2k
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Bring up the CAN bus

Once per boot (250 kbit/s for NMEA2000). `restart-ms 100` makes the controller
auto-recover from a bus-off instead of staying down:

```bash
sudo ip link set can0 up type can bitrate 250000 restart-ms 100   # FDCAN1
# sudo ip link set can1 up type can bitrate 250000 restart-ms 100 # FDCAN2
ip -details link show can0    # want: state ERROR-ACTIVE, bitrate 250000
```

If `can0` shows `BUS-OFF`, fix that before expecting output to land — a CAN frame
needs at least one other node on the wire to acknowledge it (check termination
≈60 Ω, common ground, and CAN-H/CAN-L not swapped).

## Run

Activate the venv first (`source .venv/bin/activate`), then:

```bash
# replay a captured Fastnet hex file (safest first test)
python -m fastnet2n2k --file capture.txt --channel can0

# live from the Fastnet bus
python -m fastnet2n2k --serial /dev/ttyUSB0 --channel can0
```

Find your serial adapter with `ls /dev/ttyUSB* /dev/ttyACM* /dev/ttyS*`. Stop with
Ctrl-C.

Options:

| Option | Default | Meaning |
|---|---|---|
| `--serial DEV` / `--file PATH` | — | input source (one is required) |
| `--channel` | `can0` | SocketCAN interface |
| `--n2k-src` | `22` | preferred N2K source address (0–251) |
| `--n2k-priority` | per-PGN standard | override CAN priority (0–7, 0=highest) for **all** transmitted frames; **if omitted, each PGN keeps its standard priority** (see the PGN table above) |
| `--unique` | from hostname | device NAME unique number |
| `--live-data` | off | print the live channel table to the console once per second |
| `--log-level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

**CAN failure handling:** the device reconnects automatically. If `can0` isn't up at
start it waits (logging retries) rather than exiting; if the bus drops or goes bus-off
mid-run, sends fail quietly (logged at most every 5 s) and resume once it recovers — the
bridge keeps running. Use `--log-level DEBUG` to see connection/retry detail.

## Verify

Watch the raw frames on the board with `can-utils`
(`sudo apt install can-utils`):

```bash
candump -ta can0
```

You should see 29-bit frames appear as instruments update — heading, wind, depth,
speed, etc. — and stop when they go quiet. On a connected chartplotter / analyzer
the device appears in the device list after its ISO address claim (PGN 60928).

### Test the whole chain with a loopback (no instruments needed)

With `can0` and `can1` wired together (CAN-H↔CAN-H, CAN-L↔CAN-L, one 120 Ω
terminator), `can1` provides the ACK so `can0` can transmit:

```bash
sudo ip link set can1 up type can bitrate 250000 restart-ms 100
candump -ta can1                                              # terminal 1
python -m fastnet2n2k --file capture.txt --channel can0      # terminal 2
```

## Run at boot (systemd)

A unit file is provided in [`fastnet2n2k.service`](fastnet2n2k.service). Edit the
paths, user and `--serial` device to match your install, then:

```bash
sudo cp fastnet2n2k.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fastnet2n2k.service
journalctl -u fastnet2n2k.service -f      # watch logs
```

The unit brings `can0` up itself (with `restart-ms 100`) before starting, and
restarts the bridge on failure.

## Tests

```bash
source .venv/bin/activate
pip install pytest
python -m pytest tests/ -q
```

The suite drives the mapping with pyfastnet's bundled capture files and round-trips
the resulting NMEA2000 messages to assert PGNs, unit conversions, T/M references,
sign passthrough, the send throttle, and the full file→decode→send pipeline.

---

## Sender POC (`nmea2000_poc.py`)

A standalone minimal proof-of-concept that transmits a single NMEA2000 PGN (127250
Vessel Heading) onto the bus — useful for smoke-testing a CAN link independently of
the Fastnet pipeline.

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
