"""Optional live path table for the console (--live-data).

Clears the screen and prints every Signal K path currently in the live store with its
latest SI value and age, so you can eyeball what the decoder is producing while the
bridge runs. (pyfastnet v3 emits SI values only; display_text/layout no longer exist
downstream.)
"""

import time

from .live_store import live_data


def print_live_data(fb):
    print("\033c", end="")   # clear screen
    now = time.monotonic()
    hdr = f"{'Signal K path':<52} {'Value (SI)':<26} {'Age(s)':<8}"
    print(hdr)
    print("-" * len(hdr))
    for path, data in sorted(live_data.items()):
        age = f"{now - data['timestamp']:.1f}"
        print(f"{str(path):<52} {str(data['value']):<26} {age:<8}")
    print(f"Buffer: {fb.get_buffer_size()}\n")
