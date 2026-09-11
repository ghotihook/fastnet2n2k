# The RS485 TX pin jams the Fastnet bus during boot

**Status: root cause confirmed on hardware.** Fixed by one line in `config.txt`. This is
a *deployment* fix, not a code fix — there is nothing in this repo to change, and a
fresh install on new hardware needs the step in [The fix](#the-fix) or it will hit this.

## The problem

Rebooting the bridge while the instruments are already running and settled makes the
B&G H2000/Hydra instruments raise an alarm. The alarm latches: it stays on (sounder
beeping ~1 s) until the bridge is powered down, and returns as soon as it is powered
back up.

What made it confusing:

- Powering the instruments and the bridge up **together** is fine — no alarm, every time.
- **Fastnet data keeps flowing** the whole time the alarm is sounding. The bridge decodes
  and transmits normally.
- The alarm is on the **Fastnet** side. The bridge's NMEA 2000 output is not involved.
- Nothing in this repo transmits on Fastnet. `SerialReader` (`fastnet2n2k/input_source.py`)
  only ever calls `read()`; `git log -S"ser.write" --all` returns zero commits, and
  pyfastnet's decoder has no I/O handle at all.

The obvious suspects — serial misalignment, a termination resistor, a ground problem —
are all wrong, and one observation rules out the whole passive-hardware class:

> **A warm reboot never drops the transceiver's supply.** Termination, failsafe bias and
> receiver input loading are electrically *identical* before, during and after a reboot.
> None of them can be the trigger. Only something the SoC does to its pins can change.

## Why it happens

Three facts combine.

**1. The Waveshare RS485 transceiver has automatic direction control.** From Waveshare's
own description of the circuit (documented on the RS485 CAN HAT wiki; it is their house
design across these boards):

> "When P_TX is pulled low, indicating the start of data transmission, the transistor is
> cut off, and the DE pin is set to a high level, enabling data transmission."

So **TX low → driver enabled**. TX idle high → driver disabled, high-Z. That is why the
bridge is an invisible listener while running: TX sits at mark, the driver never turns on.

**2. The Fastnet tap is on RS485 channel CH2, whose TX is GPIO12.** The board is a
Waveshare IPCBOX-CM5-A running a **CM4**, so every vendor instruction naming a `-pi5`
overlay has to be translated to its BCM2711 equivalent. The pins are the same; only the
UART numbering differs:

| Ch | GPIO TX/RX | CM5 overlay (vendor docs) | CM4 overlay | CM4 device |
|---|---|---|---|---|
| CH0 | 4 / 5 | `uart2-pi5` | `uart3` | `/dev/ttyAMA3` |
| CH1 | 8 / 9 | `uart3-pi5` | `uart4` | `/dev/ttyAMA4` |
| **CH2** | **12 / 13** | `uart4-pi5` | **`uart5`** | **`/dev/ttyAMA5`** ← Fastnet |
| CH3 | 14 / 15 | `uart0-pi5` | `uart0` | `serial0` / `/dev/ttyAMA0` |

**3. GPIO12 comes out of reset pulled LOW.** The BCM2711 pad reset defaults split at
GPIO8: **GPIO0–8 default to pull-up, GPIO9–27 to pull-down.** GPIO12 is in the pull-down
range, and nothing in `config.txt` said otherwise.

Put together:

1. From SoC reset until the `uart5` overlay applies ALT4 — seconds, not microseconds —
   GPIO12 is an unmuxed input held low by its internal pull-down.
2. The auto-direction circuit reads that as "transmitting", raises DE, and the
   transceiver **drives a continuous space onto the Fastnet pair for the whole boot
   window**.
3. The H2000 master loses its poll replies mid-cycle and **latches** an alarm.
4. The kernel then muxes ALT4, TX idles high, DE drops, the driver goes high-Z. The bus
   is healthy again and data flows — but the alarm is already latched, which is exactly
   the "alarm on, data fine" combination that made this look impossible.
5. Powering the unit off unpowers the transceiver, releasing the bus; the master
   re-finds every instrument and the alarm clears. Powering on repeats the boot window.
6. Powering everything up together works because the jam lands while the instruments are
   still booting, before the master runs its own bus scan.

Note the coincidence that made this bite: **CH2 is the only one of the four RS485
channels whose TX pin sits in the pull-down range.** CH0 (GPIO4) and CH1 (GPIO8) come up
pulled high and keep their drivers off through the entire boot.

## The fix

One line in `/boot/firmware/config.txt`:

```
gpio=12=op,dh
```

The firmware applies this before the kernel loads, so GPIO12 is driven **high** — the
UART idle/mark level — from early boot. DE never rises, the driver never turns on. The
overlay then takes the pin to ALT4, where it idles high anyway, so the wire sees a
continuous mark across the whole boot with no state change at all.

Verified on hardware: the alarm no longer fires on reboot.

If you move the Fastnet tap to a different RS485 channel, change the pin number to that
channel's TX from the table above — and note that CH0 and CH1 don't need the line, since
their pins are already pulled high at reset.

### The stronger variant

`gpio=` only takes effect once the firmware has read `config.txt`, so a short window
remains between SoC reset and firmware init. A hardware pull-up has no window at all.
**Moving the Fastnet pair to CH0's terminals** (and pointing `--serial` at
`/dev/ttyAMA3`) removes the problem structurally rather than patching it. The one-line
fix has proved sufficient in practice; this is the belt-and-braces option if the glitch
ever reappears.

## Also worth knowing on this board

- **The serial console sits on RS485 channel CH3.** GPIO14/15 is CH3's transceiver, and
  a stock Pi OS install has `enable_uart=1` plus `console=serial0,115200` in
  `cmdline.txt`. Every boot therefore writes 115200-baud console text into CH3's
  transceiver, auto-enabling *its* driver on every low bit. Harmless while CH3 is
  unwired — a bus jammer the moment it isn't. To disable: drop the `console=serial0,115200`
  token from `cmdline.txt` (keep `console=tty1`) and
  `sudo systemctl disable --now serial-getty@ttyAMA0`. That costs you the serial rescue
  console.
- **`dtoverlay=uart2` does nothing useful on a CM4.** On BCM2711 that is GPIO0/1, the ID
  EEPROM pins — not an RS485 channel. It appears in configs copied from CM5 guides where
  the numbering is different.
- **The 120R jumper** is shared between RS485 and CAN. 120 Ω across a low-drive Fastnet
  pair is a heavy load. It cannot cause this fault (it is passive, and unaffected by a
  warm reboot) but it erodes signal margin — worth leaving off for a listen-only tap.

## Re-confirming it, if this ever comes back

With the instruments running and settled, provoke the fault deliberately:

```bash
sudo systemctl stop fastnet2n2k
printf 'UUUUUUUUUUUUUUUU' | sudo tee /dev/ttyAMA5 > /dev/null
```

If the alarm fires, the channel's driver is live and reaching the bus. `0x55` is
alternating bits, so it disturbs the pair regardless of what baud the port is at. Clear
the alarm by power-cycling the unit, as usual. Do this alongside, not underway.

To check the steady-state pin muxing (`pinctrl` replaces the older `raspi-gpio`):

```bash
pinctrl get 12-15     # want: 12: a4 ... | hi   — ALT4 TXD5, idling high
```

## Scope

Applies to any Fastnet tap on a Waveshare auto-direction RS485 channel whose TX pin
falls in the BCM2711 pull-down range (GPIO9–27) — not just this board and not just this
bridge. Any receive-only listener on such a channel will jam its bus for the length of
every boot. The bridge itself is blameless: it never transmits, and the fault is over
before its process starts.
