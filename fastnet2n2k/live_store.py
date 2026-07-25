"""Latest-value store for decoded Fastnet data (pyfastnet v3).

Single-threaded: written and read from the main decode loop only. pyfastnet v3 emits
``{signalk_path: SI_value}``, so each entry keys a Signal K path to its latest SI
value plus a monotonic ``timestamp`` for freshness (age). Monotonic, not wall-clock:
written on every update and only ever read as an elapsed delta, so it must be cheap
and immune to clock steps.
"""

import time

live_data: dict = {}


def update_live_data(path, value):
    live_data[path] = {"value": value, "timestamp": time.monotonic()}


def get_live_data(path):
    entry = live_data.get(path)
    return entry["value"] if entry else None
