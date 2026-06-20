"""Optional live channel table for the console (--live-data).

Ported from fastnet2ip/core/display.py. Clears the screen and prints every channel
currently in the live store with its value, display text, layout and age, so you can
eyeball what the decoder is producing while the bridge runs.
"""

import time

from .live_store import live_data


def print_live_data(fb):
    print("\033c", end="")   # clear screen
    now = time.monotonic()
    hdr = f"{'Channel':<35} {'ID':<10} {'Value':<20} {'Display':<20} {'Layout':<12} {'Age(s)':<10}"
    print(hdr)
    print("-" * len(hdr))
    for name, data in sorted(live_data.items()):
        ts = data.get("timestamp")
        age = f"{now - ts:.1f}" if ts is not None else ""
        print(
            f"{str(name):<35} {str(data.get('channel_id', '')):<10} "
            f"{str(data.get('value')):<20} "
            f"{str(data.get('display_text')):<20} "
            f"{str(data.get('layout', '')):<12} "
            f"{age:<10}"
        )
    print(f"Buffer: {fb.get_buffer_size()}\n")
