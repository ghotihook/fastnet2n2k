"""Latest-value store for decoded Fastnet channels.

Single-threaded: written and read from the main decode loop only. Each entry keeps
the decoded ``value``, the ``display_text`` and ``layout`` from pyfastnet (the layout
carries the T/M reference), and a monotonic ``timestamp`` so freshness (age) can be
reasoned about. Monotonic, not wall-clock: this is written on every channel update and
only ever read as an elapsed delta, so it must be cheap and immune to clock steps.

Ported from fastnet2ip/core/data_store.py.
"""

import time

live_data: dict = {}


def update_live_data(channel_name, channel_id, value, display_text, layout):
    live_data[channel_name] = {
        "channel_id":   channel_id,
        "value":        value,
        "display_text": display_text,
        "layout":       layout,
        "timestamp":    time.monotonic(),
    }


def get_live_data(name):
    entry = live_data.get(name)
    return entry.get("value") if entry else None


def get_live_display(name):
    entry = live_data.get(name)
    return entry.get("display_text") if entry else None
