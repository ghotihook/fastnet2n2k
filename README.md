# fastnet2n2k — B&G Fastnet → NMEA2000 bridge (M5Stack CoreMP135)

Reads a **B&G Fastnet** instrument stream (live serial or a captured hex file),
decodes it with [`pyfastnet`](https://github.com/ghotihook/pyfastnet), maps the
channels to **NMEA2000** PGNs and transmits them onto a CAN bus from an
[M5Stack CoreMP135](https://docs.m5stack.com/en/core/M5CoreMP135).

```bash
# bring the bus up (250 kbit/s for NMEA2000), then:
python -m fastnet2n2k --serial /dev/ttyUSB0 --channel can0   # live Fastnet
python -m fastnet2n2k --file capture.txt    --channel can0   # replay a capture
```

### What it sends
Each Fastnet channel is mapped to the matching PGN and emitted **only when the
channel updates** (with a 0.05 s min interval and a 5 s max re-broadcast), so when
the instruments go quiet the output stops and consumers time the data out:

| Data | PGN | Notes |
|---|---|---|
| Heading | 127250 | T/M reference taken from the Fastnet display layout (`°M`/`°T`) |
| Apparent / True wind, TWD | 130306 | knots→m/s, deg→rad; reference per layout |
| Boat speed | 128259 | knots→m/s |
| Depth | 128267 | metres |
| COG/SOG | 129026 | prefers True COG, falls back to Magnetic |
| Attitude (heel/trim) | 127257 | signed value passed through unchanged |
| Rudder, Leeway, Rate of turn | 127245 / 128000 / 127251 | |
| Distance log, XTE | 128275 / 129283 | NM→m |
| Position | 129025 | |
| Sea / air temperature | 130312 | °C/°F→Kelvin |
| Barometric pressure | 130314 | mbar→Pa |

**Units** are converted to NMEA2000 SI. **Sign** comes straight from pyfastnet's
decoded value. **True/Magnetic** is read from the pyfastnet `layout` field (the only
place it exists); a bearing whose layout can't be resolved is skipped, never guessed.
Tidal Set/Drift (129291) and the B&G proprietary raw PGNs (65280–65282) are
deferred (the `n2k` library has no builders for them yet).

---

## Sender POC (`nmea2000_poc.py`)

Minimal proof-of-concept that writes NMEA2000 messages (PGN 127250 Vessel
Heading) onto a CAN/NMEA2000 network from an [M5Stack CoreMP135](https://docs.m5stack.com/en/core/M5CoreMP135).

The CoreMP135 runs Linux on an STM32MP135 and has two FDCAN interfaces
(SIT1051T transceivers): **FDCAN1** (PE3 TX / PE10 RX) and **FDCAN2**
(PG0 TX / PE0 RX). Under Linux these appear as the SocketCAN netdevs `can0` /
`can1`. NMEA2000 runs at **250 kbit/s** with 29-bit extended CAN IDs.

Message encoding, CAN-ID construction, fast-packet framing and ISO address
claiming are handled by the [`n2k`](https://github.com/finnboeger/NMEA2000)
library (on top of `python-can`'s socketcan backend).

## Setup

Bring up the CAN interface at the NMEA2000 bitrate (once per boot):

```bash
sudo ip link set can0 up type can bitrate 250000      # FDCAN1
# sudo ip link set can1 up type can bitrate 250000    # FDCAN2
ip -details link show can0                             # verify state + bitrate
```

Create a virtualenv and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python nmea2000_poc.py --channel can0 --heading 90 --once   # single frame
python nmea2000_poc.py --channel can0 --heading 90          # ~10 Hz loop
```

Options: `--channel` (default `can0`), `--heading` degrees, `--ref true|magnetic`,
`--rate` Hz, `--once`.

## Verify

On the connected receiver (chartplotter / analyzer / MFD) confirm a **PGN 127250
Vessel Heading** of the requested value appears, and that the device shows up in
the device list after its address claim (PGN 60928).

On the board itself you can watch the raw frames with `can-utils`:

```bash
candump can0
```

### Desk testing without a bus

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
candump vcan0                                            # terminal 1
python nmea2000_poc.py --channel vcan0 --heading 90 --once   # terminal 2
```
