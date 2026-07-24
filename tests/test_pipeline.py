"""Coverage smoke test: replaying a real capture produces the expected PGNs.

Feeds pyfastnet's example capture through the decoder into the live store and runs
every channel's trigger, asserting the core sailing PGNs are emitted and that each
emitted message encodes through the canboat codec. (The CAN device/transport is
covered separately; here we exercise decode → live store → mapping.)
"""

from nmea2000.encoder import NMEA2000Encoder
from nmea2000.input_formats import N2KFormat
import nmea2000.encoder_formats  # noqa: F401

from fastnet2n2k import mapping
from tests.test_mapping import load_capture

_ENC = NMEA2000Encoder(N2KFormat.CAN_FRAME_ASCII)


def test_full_capture_emits_expected_pgns():
    load_capture("example1_fastnet_data.txt")
    pgns = set()
    for channel_name in mapping._CHANNEL_MAP:
        msg = mapping.trigger_n2k_frame(channel_name)
        if msg is None:
            continue
        assert _ENC.encode(msg), f"{channel_name} (PGN {msg.PGN}) failed to encode"
        pgns.add(msg.PGN)

    for expected in {127250, 130306, 128259, 128267, 129026, 130312, 127245,
                     129025, 129291}:
        assert expected in pgns, f"missing PGN {expected}; got {sorted(pgns)}"
