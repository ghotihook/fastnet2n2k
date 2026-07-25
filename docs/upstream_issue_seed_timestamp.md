# Draft issue for tomer-w/nmea2000

Post at: https://github.com/tomer-w/nmea2000/issues

---

**Title:** `_seed_network_map` uses a string timestamp, crashing `can.Message` logging at DEBUG

**Body:**

`ioclient.py`'s `_seed_network_map()` builds the network-map seed messages from a
hard-coded JSON blob whose `timestamp` is a **string**:

```
'{"PGN":59904, ... ,"timestamp":"2012-06-17T15:02:11", ... }'
```

`NMEA2000Message.timestamp` therefore becomes a string, and it flows through encoding
into the `can.Message` objects handed to python-can. python-can's contract is that
`Message.timestamp` is a float (Unix epoch seconds), and its `Message.__str__`
formats it with `%f`. So any code path that stringifies one of these messages raises:

```python
>>> import can
>>> m = can.Message(arbitration_id=0x123, data=b"\x01")
>>> m.timestamp = "2012-06-17T15:02:11"
>>> str(m)
ValueError: Unknown format code 'f' for object of type 'str'
```

This fires in practice at `logging` level DEBUG: python-can's socketcan backend logs
`logger_tx.debug("sending: %s", msg)` (socketcan.py), so each seed send emits a
`--- Logging error ---` traceback rather than the intended line.

**Versions:** nmea2000 2026.5.2, python-can 4.6.1, Python 3.13, Linux.

**Suggested fix:** give the seed blob a numeric timestamp (e.g. `time.time()`, or
`0.0`), or set `msg.timestamp` to a float before sending, so `can.Message.timestamp`
holds the float type python-can expects.

**Impact:** cosmetic (a logging-time crash, only at DEBUG; the seed messages are still
sent), but it makes DEBUG logs hard to read — which is exactly when you want them.

---

## Optional secondary note (lower priority — verbosity, not a bug)

Also in `ioclient.py`: a transient, internally-retried "python-can transmit queue
full, retrying send" is logged at **WARNING with `exc_info=`** (a full traceback) on
every retry. When the bus is congested at a normal frame rate this floods the journal
with tracebacks for a condition that recovers on its own. Consider DEBUG, or a
rate-limited/summary log, for the retried case, reserving WARNING/ERROR for a send
that ultimately fails. (Raise separately from the timestamp bug above.)
