"""A PNG CRC + successful zlib inflation is not proof of image content."""
import struct
import zlib
from pathlib import Path
from test_r65_windows_evidence_gate import verifier, sample_png


def chunk(kind, value):
    return (struct.pack(">I", len(value)) + kind + value
            + struct.pack(">I", zlib.crc32(kind + value) & 0xffffffff))


def fake_png(data, *, width=128, height=128, color=2):
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(data))
            + chunk(b"IEND", b""))


def test_complete_real_image_passes(tmp_path):
    p = tmp_path / "valid.png"
    p.write_bytes(sample_png())
    assert verifier().valid_png_capture(p)


def test_zlib_garbage_with_correct_crc_rejected(tmp_path):
    p = tmp_path / "fake.png"
    p.write_bytes(fake_png(b"garbage"))
    assert not verifier().valid_png_capture(p)


def test_wrong_scanline_length_rejected(tmp_path):
    p = tmp_path / "fake.png"
    p.write_bytes(fake_png(b"\x00" * (128 * (1 + 128 * 3) - 1)))
    assert not verifier().valid_png_capture(p)


def test_invalid_row_filter_rejected(tmp_path):
    p = tmp_path / "fake.png"
    p.write_bytes(fake_png((b"\x05" + b"\x20" * (128 * 3)) * 128))
    assert not verifier().valid_png_capture(p)


def test_interlace_not_silently_treated_as_decoded(tmp_path):
    p = tmp_path / "fake.png"
    original = sample_png()
    # Use fully rehashed IHDR for unsupported Adam7 rather than bad CRC.
    raw = original[33:-12]
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 2, 0, 0, 1))
           + raw)
    p.write_bytes(png)
    assert not verifier().valid_png_capture(p)
