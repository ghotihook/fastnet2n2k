"""Optional live channel table for the console (--live-data).

Ported from fastnet2ip/core/display.py. Clears the screen and prints every channel
currently in the live store with its value, layout and age, so you can eyeball what
the decoder is producing while the bridge runs.
"""

from datetime import datetime, timezone

from .live_store import live_data


def print_live_data(fb):
    print("\033c", end="")   # clear screen
    now = datetime.now(timezone.utc)
    hdr = f"{'Channel':<35} {'ID':<10} {'Value':<20} {'Layout':<12} {'Age(s)':<10}"
    print(hdr)
    print("-" * len(hdr))
    for name, data in sorted(live_data.items()):
        ts = data.get("timestamp")
        val = data.get("value")
        display = str(val) if val is not None else data.get("display_text", "")
        age = f"{(now - ts).total_seconds():.1f}" if ts else ""
        print(
            f"{str(name):<35} {str(data.get('channel_id', '')):<10} "
            f"{display:<20} {str(data.get('layout', '')):<12} {age:<10}"
        )
    print(f"Buffer: {fb.get_buffer_size()}\n")
