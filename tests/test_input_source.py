"""Opening the two Fastnet input sources.

run() catches OSError and ValueError from these and turns them into a clean exit
message, so the exception *types* are part of the contract, not just the happy path.
"""

import pytest

from fastnet2n2k.input_source import READ_SIZE, load_capture_file


def write(tmp_path, text, name="capture.txt"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_loads_a_capture_ignoring_spaces_and_newlines(tmp_path):
    """Captures are usually written one frame per line, with spaces between bytes."""
    path = write(tmp_path, "01 02 03\n04 05 06\n")
    assert b"".join(load_capture_file(path)) == bytes([1, 2, 3, 4, 5, 6])


def test_splits_into_read_size_chunks(tmp_path):
    path = write(tmp_path, "ab" * (READ_SIZE + 5))
    chunks = list(load_capture_file(path))
    assert [len(c) for c in chunks] == [READ_SIZE, 5]


def test_empty_file_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        load_capture_file(write(tmp_path, "   \n\n"))


def test_non_hex_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        load_capture_file(write(tmp_path, "not hex at all"))


def test_missing_file_raises_os_error(tmp_path):
    with pytest.raises(OSError):
        load_capture_file(str(tmp_path / "nope.txt"))
