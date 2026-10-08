"""The RGB-scan ordering pass reads each file's capture time from its tag directories only.
piexif reads the whole of a TIFF-structured raw, which on a network share is the cost of the pass."""

import io

import numpy as np
import piexif
import pytest
from PIL import Image

from negpy.features.rgbscan.logic import capture_timestamp
from negpy.infrastructure.loaders.helpers import _tiff_capture_datetime, read_capture_datetime

_ORIGINAL = b"2026:09:19 12:34:56"
_MODIFIED = b"2020:01:01 00:00:00"


class _CountingFile(io.FileIO):
    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.bytes_read = 0

    def read(self, *args):
        data = super().read(*args)
        self.bytes_read += len(data)
        return data

    def readinto(self, buffer):
        n = super().readinto(buffer)
        self.bytes_read += n or 0
        return n


def _save(path, fmt="TIFF", **tags):
    ifds = {"0th": {}, "Exif": {}}
    if "modified" in tags:
        ifds["0th"][piexif.ImageIFD.DateTime] = tags["modified"]
    if "original" in tags:
        ifds["Exif"][piexif.ExifIFD.DateTimeOriginal] = tags["original"]
    pixels = (np.random.default_rng(0).random((600, 600, 3)) * 255).astype("uint8")
    Image.fromarray(pixels).save(path, format=fmt, exif=piexif.dump(ifds))
    return str(path)


def test_reads_date_time_original_without_reading_the_pixels(tmp_path):
    path = _save(tmp_path / "f.tif", original=_ORIGINAL, modified=_MODIFIED)
    with _CountingFile(path, "rb") as source:
        assert _tiff_capture_datetime(source) == _ORIGINAL.decode()
        assert source.bytes_read < 64 * 1024


def test_falls_back_to_the_ifd0_date_time(tmp_path):
    assert read_capture_datetime(_save(tmp_path / "f.tif", modified=_MODIFIED)) == _MODIFIED.decode()


def test_a_tiff_that_states_no_time_reads_empty(tmp_path):
    assert read_capture_datetime(_save(tmp_path / "f.tif")) == ""


@pytest.mark.parametrize("content", [b"", b"FUJIFILMCCD-RAW 0201", b"II*\x00garbage"])
def test_a_file_it_cannot_parse_defers_to_the_full_reader(tmp_path, content):
    path = tmp_path / "f.raw"
    path.write_bytes(content)
    assert read_capture_datetime(str(path)) is None


def test_capture_timestamp_agrees_with_the_full_exif_read(tmp_path):
    tiff = _save(tmp_path / "f.tif", original=_ORIGINAL)
    jpeg = _save(tmp_path / "f.jpg", fmt="JPEG", original=_ORIGINAL)
    assert read_capture_datetime(jpeg) is None
    assert capture_timestamp(tiff) == capture_timestamp(jpeg) == "2026-09-19 12:34:56"
