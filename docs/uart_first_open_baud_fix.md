# Proposed fix: the first serial open after boot runs at the wrong baud

## The problem

On the CM4, the **first** open of `/dev/ttyAMA5` after a boot does not program the
requested baud rate into the PL011 hardware. The port stays at the tty default of
9600 while `termios` reports 28800. Every open after that is correct.

Receiving a 28800-baud stream with the hardware at 9600 captures roughly **one byte in
three**, and those are garbled. That is the "first run after boot reads nothing"
symptom: closing and re-running the bridge fixed it only because the *second* open
programs the divisor properly.

## Evidence

Measured over six cold boots with a known counter pattern (0,1,2,…,255,0,…) sent from
`sk` `/dev/ttyUSB0` to `cm4` `/dev/ttyAMA5`. A counter is what made this findable:
replaying Fastnet can only report "didn't decode", whereas a known sequence separates
*bytes lost* from *bytes corrupted*.

| First open after boot | Byte rate | Result |
|---|---|---|
| plain (what the bridge does today) | 821 B/s — 34.2% of wire | 0/112 windows clean, thousands of bytes lost |
| re-assign the *same* baud | 821 B/s | still broken |
| **bounce the baud away and back** | **2395 B/s — 99.8%** | **111/112 windows clean, 0 lost** (2 of 2 boots) |

Three independent confirmations that the hardware is at 9600 while termios claims 28800:

1. 821/2400 B/s is exactly **one third**, and 28800 ÷ 9600 = 3.
2. Requesting **86400** on the first open produced a byte-identical broken result to
   requesting 28800 — the requested value has no effect at all on that open.
3. `TCGETS2` read back the requested speed correctly every time, so the termios layer
   agrees with us; only the hardware divisor is wrong.

Not a clock problem: `vcgencmd measure_clock uart` reads 48 MHz before anything opens
the port.

### The trigger is the *non-standard* baud rate

Control test, same cold-boot procedure but with a **standard** rate (38400) on both
ends: the first open is clean — 3189 B/s against 3200 expected, 148/149 windows clean,
zero bytes lost. Same board, same cable, same carrier, same first open.

| first open after boot | rate | result |
|---|---|---|
| 28800 (non-standard) | 34% of wire | broken |
| 38400 (standard) | 99.7% of wire | clean |

So the fault is specific to the custom-baud code path, not to the hardware. It also
clears the Waveshare CM4 carrier board and the wiring of any suspicion.

## Why it happens

28800 is not a standard rate on Linux — there is no `B28800`, so pyserial configures it
through the `BOTHER` custom-divisor path: it calls `tcsetattr` with `CBAUD = BOTHER`,
then sets the literal rate with a `TCSETS2` ioctl.

This trips a long-known behaviour in the kernel's serial core. `uart_set_termios()`
skips reprogramming the hardware when nothing "relevant" has changed — and its notion
of "relevant" looks at the `c_cflag` baud bits, not at `c_ispeed`/`c_ospeed`. With
`BOTHER` those bits are identical before and after, so the actual speed change is
optimised away and never reaches the divisor. Meanwhile, when the `BOTHER` speed
doesn't propagate, serial core falls back to a default of **9600** — precisely the rate
we measured.

That explains every observation, including the odd one: re-assigning the *same* baud
does nothing because there is no cflag change at all, whereas bouncing to 9600 (a
standard rate, so the CBAUD bits genuinely change) and back to 28800 forces two real
changes and the divisor gets written.

Background reading:

- [\[SERIAL\] Don't optimise away baud rate changes when BOTHER is used](https://lkml.iu.edu/hypermail/linux/kernel/0706.1/0193.html) — the optimisation and why it skips `c_[io]speed`
- [serial: core: Fix initializing and restoring termios speed](https://lkml.kernel.org/lkml/20211115165316.841485448@linuxfoundation.org/) — the 9600 fallback when a `BOTHER` speed is lost
- [pyserial #805: custom baudrates and `BOTHER`/`tcsetattr`](https://github.com/pyserial/pyserial/issues/805) — the userspace side of the same path
- [usb console: pass initial console baud on to first tty open](https://lkml.iu.edu/hypermail/linux/kernel/0909.0/01869.html) — the same "first open defaults to 9600" shape in another driver

## The fix

In `fastnet2n2k/input_source.py`, in `open_serial_port()`, force one genuine termios
change after opening:

```python
def open_serial_port(device):
    """Open the Fastnet serial port: 28800 baud, 8 data bits, odd parity, 2 stop."""
    logger.info("Serial port: %s", device)
    ser = serial.Serial(port=device, baudrate=BAUDRATE, bytesize=BYTE_SIZE,
                        stopbits=STOP_BITS, parity=PARITY, timeout=0)

    # The first open of a PL011 after a boot does not program the requested baud into
    # the hardware — it stays at the tty default of 9600 while termios reports 28800,
    # so ~2/3 of the bytes are lost and the rest are garbage. Bouncing the rate forces
    # a real termios change, which programs the divisor properly. Re-assigning the
    # SAME value does nothing: pyserial skips the reconfigure when it hasn't changed.
    ser.baudrate = 9600
    ser.baudrate = BAUDRATE
    return ser
```

Two lines, no retry loop, no timeout, no watchdog.

### Why not the alternatives

- **Re-assign the same baud** — measured, does not work. pyserial short-circuits it.
- **Close and reopen** — works, and is what the old reopen watchdog was accidentally
  doing, but it costs a full open cycle and needs a policy for when to trigger.
- **A reopen watchdog** — treats the symptom without diagnosing it, and costs ~20 s of
  dead bridge on every start. Removed in `533fd43`; this fix replaces it properly.

## How to verify

The counter-pattern harness lives in `~` on both hosts (`linktest.py`,
`linktest2.py`). To confirm the fix on the real bridge:

1. Start the sender on `sk`:
   `~/python_environment/bin/python ~/linktest.py send --port /dev/ttyUSB0`
   (or run the normal Fastnet playback)
2. Reboot `cm4`.
3. As the **first** thing after boot, run the bridge and watch for decoded frames
   within a second or two, with no reopen and no garbage phase.

To re-measure the underlying fault directly:

```
# on cm4, first open after a boot
~/python_environment/bin/python ~/linktest2.py seq --port /dev/ttyAMA5 --plan plain,plain
# 821 B/s then 2395 B/s == fault reproduced

~/python_environment/bin/python ~/linktest2.py seq --port /dev/ttyAMA5 --plan reset
# 2395 B/s on the first open == fix confirmed
```

## Scope

This is a property of the Pi's PL011 UART, not of Fastnet or of this bridge. Any
program opening `/dev/ttyAMA[0-5]` at a non-default rate as the first open after a
boot will hit it, so the same two lines are worth applying anywhere else that opens
these ports — including `pb.py` on the sending side.
