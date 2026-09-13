# Draft issue for tomer-w/nmea2000

Post at: https://github.com/tomer-w/nmea2000/issues

---

**Title:** A send that exhausts its transmit-queue-full retries logs a traceback *and* re-raises, once per frame

**Body:**

When the CAN transmit buffer stays full (congested bus, or no other node ACKing),
the python-can client retries each frame (`send_retry_count`, logged at DEBUG since
2026.8, thanks!) and then re-raises the `CanOperationError`. `_is_transient_send_error`
classifies it as transient, so `AsyncIOClient.send()` takes the no-reconnect branch:

```python
self.logger.warning(
    "Send failed without reconnecting. Error %s", ex, exc_info=True
)
raise
```

The caller already receives the exception, so logging it with a full traceback first
reports the same failure twice. More importantly, on a dead or saturated bus it fires
for *every* frame. At a normal transmit rate (tens of frames/s) that is tens of
tracebacks per second in the journal, burying everything else exactly when you're
trying to diagnose the bus.

**Versions:** nmea2000 2026.8.1, python-can 4.6.1, Linux (SocketCAN).

**Suggested fix:** since the exception is re-raised, either don't log it here (leave it
to the caller), or log a single line at DEBUG/WARNING without `exc_info`. Rate-limiting
would also work.

**Impact:** verbosity, not a functional bug. But downstream users currently have to
filter it out by message text, which breaks silently if the wording changes.

---

(An earlier draft here reported two other things: the per-retry "transmit queue full"
WARNING with a traceback, and the network-map seed messages' string `timestamp`
crashing python-can's DEBUG logging. nmea2000 2026.8 fixed both.)
