"""Latest-value store for decoded Fastnet channels.

Single-threaded: written and read from the main decode loop only. Each entry keeps
the decoded ``value``, the ``display_text`` and ``layout`` from pyfastnet (the layout
carries the T/M reference), and a ``timestamp`` so freshness can be reasoned about.

Ported from fastnet2ip/core/data_store.py.
"""

from datetime import datetime, timezone

live_data: dict = {}


def update_live_data(channel_name, channel_id, value, display_text, layout):
    live_data[channel_name] = {
        "channel_id":   channel_id,
        "value":        value,
        "display_text": display_text,
        "layout":       layout,
        "timestamp":    datetime.now(timezone.utc),
    }


def get_live_data(name):
    entry = live_data.get(name)
    return entry.get("value") if entry else None


def get_live_display(name):
    entry = live_data.get(name)
    return entry.get("display_text") if entry else None
