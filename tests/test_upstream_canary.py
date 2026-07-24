"""Canary for the upstream nmea2000 bug that _QuietTransientCanErrors works around.

nmea2000 builds its network-map seed messages from a hard-coded JSON blob whose
``timestamp`` is a **string** (``"2012-06-17T15:02:11"``). python-can documents
``Message.timestamp`` as a float, so that string flows into a ``can.Message`` and any
attempt to stringify it — python-can's own DEBUG ``"sending: %s"`` log — crashes in
``can.Message.__str__``. Case 2 of ``_QuietTransientCanErrors`` (``__main__.py``)
drops those unrenderable records so a DEBUG journal stays readable.

**These tests are a tripwire, not a check of our own code.** They pass while the
upstream bug exists and FAIL when tomer-w fixes it. When one fails:

  1. delete case 2 of ``_QuietTransientCanErrors`` (the unrenderable-record drop),
  2. delete this file,
  3. and, if nothing else needs it, drop the whole filter.

Filed issue / full write-up: ``docs/upstream_issue_seed_timestamp.md``.
The two tests cover the two places it could be fixed — the blob itself, or coercion
in ``from_json`` — so whichever way it lands, we're told.
"""

import inspect
import re

import can
import pytest
from nmea2000.message import NMEA2000Message

# A faithful copy of the seed blob nmea2000 sends (ioclient.py, _seed_network_map).
# Only the string timestamp matters here; the rest is what makes it a valid message.
SEED_JSON = (
    '{"PGN":59904,"id":"isoRequest","description":"ISO Request","fields":['
    '{"id":"pgn","name":"PGN","description":null,"unit_of_measurement":null,'
    '"value":60928,"raw_value":60928,"physical_quantities":null,"type":[13],'
    '"part_of_primary_key":false}],"source":0,"destination":255,"priority":6,'
    '"timestamp":"2012-06-17T15:02:11","source_iso_name":null,"hash":null}'
)


def test_upstream_seed_blob_still_uses_a_string_timestamp():
    """The defect at its source. Inspect nmea2000's actual seed method rather than our
    copy, so editing the blob to a numeric timestamp trips this."""
    from nmea2000.ioclient import AsyncIOClient

    try:
        src = inspect.getsource(AsyncIOClient._seed_network_map)
    except (AttributeError, OSError):
        pytest.skip("nmea2000._seed_network_map moved or is unavailable — re-check the "
                    "bug by hand rather than trusting this canary")

    stamps = re.findall(r'"timestamp"\s*:\s*("[^"]*"|[-0-9.]+)', src)
    assert stamps, ("no timestamp literal found in the seed blob; the method changed "
                    "shape — re-check whether the bug still exists before deleting the fix")
    if not all(s.startswith('"') for s in stamps):
        pytest.fail("nmea2000's seed blob now uses a NUMERIC timestamp — the upstream "
                    "bug is fixed. Remove case 2 of _QuietTransientCanErrors and delete "
                    "tests/test_upstream_canary.py.")


def test_from_json_still_passes_the_string_timestamp_through():
    """The other place it could be fixed: from_json coercing str -> float."""
    m = NMEA2000Message.from_json(SEED_JSON)
    if isinstance(m.timestamp, (int, float)):
        pytest.fail("nmea2000 now coerces the seed timestamp to a number — the upstream "
                    "bug is fixed. Remove case 2 of _QuietTransientCanErrors and delete "
                    "tests/test_upstream_canary.py.")
    assert isinstance(m.timestamp, str)


def test_the_consequence_a_can_message_with_a_string_timestamp_cannot_be_logged():
    """Documents *why* the filter exists: this is the exact crash python-can hits when
    it logs the seed message at DEBUG. Stable python-can behaviour, not a canary."""
    msg = can.Message(arbitration_id=0x123, data=b"\x01")
    msg.timestamp = "2012-06-17T15:02:11"
    with pytest.raises(ValueError):
        str(msg)
