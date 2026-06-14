"""fastnet2n2k — bridge a B&G Fastnet instrument stream onto an NMEA2000 CAN bus.

Reads Fastnet (serial or a captured hex file), decodes it with ``pyfastnet``, maps
the decoded channels to NMEA2000 PGNs and transmits them on a SocketCAN interface
via the ``nmea2000`` (tomer-w) library. Run with ``python -m fastnet2n2k ...``.
"""

__version__ = "0.1.1"
