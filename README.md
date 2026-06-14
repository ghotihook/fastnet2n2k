# NMEA2000 transmit POC — M5Stack CoreMP135

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
