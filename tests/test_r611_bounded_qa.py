"""Repeated full QA cannot be green based on stdout alone."""
import xml.etree.ElementTree as ET
from pathlib import Path
from tools.repeat_full_qa import assess_result


def junit(path, count=500, *, fail=0):
    suite = ET.Element("testsuite", tests=str(count), failures=str(fail), errors="0", skipped="0")
    for i in range(count):
        case = ET.SubElement(suite, "testcase", name=f"check_{i}")
        if fail and i == 0:
            ET.SubElement(case, "failure")
    ET.ElementTree(suite).write(path)


def test_rejects_missing_exit_even_when_junit_says_pass(tmp_path):
    file = tmp_path / "tests.xml"; junit(file)
    assert assess_result(None, file)["status"] == "FAIL"
    assert assess_result(124, file)["status"] == "FAIL"


def test_rejects_missing_or_forged_junit(tmp_path):
    file = tmp_path / "missing.xml"
    assert assess_result(0, file)["status"] == "FAIL"
    file.write_text('<testsuite tests="500" failures="0" errors="0" skipped="0" />', encoding="utf-8")
    assert assess_result(0, file)["status"] == "FAIL"


def test_rejects_inconsistent_or_failed_tests(tmp_path):
    file = tmp_path / "tests.xml"
    junit(file, fail=1)
    assert assess_result(0, file)["status"] == "FAIL"
    junit(file)
    assert assess_result(0, file, expected=501)["status"] == "FAIL"
    assert assess_result(0, file, expected=500)["status"] == "PASS"


def test_bounded_qa_refuses_empty_result(tmp_path):
    file = tmp_path / "tests.xml"; junit(file, count=2)
    assert assess_result(0, file)["status"] == "FAIL"


def test_indexed_png_requires_valid_palette(tmp_path):
    """A palette-less indexed image used to satisfy the native PNG gate."""
    import struct
    import zlib
    from tools.verify_windows import valid_png_capture

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 100, 100, 8, 3, 0, 0, 0))
    image = chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00" * 100) * 100))
    ending = chunk(b"IEND", b"")
    file = tmp_path / "forged-indexed.png"
    file.write_bytes(b"\x89PNG\r\n\x1a\n" + header + image + ending)
    assert not valid_png_capture(file)
    # A palette with at least one entry restores the valid image structure.
    palette = chunk(b"PLTE", b"\x10\x20\x30")
    file.write_bytes(b"\x89PNG\r\n\x1a\n" + header + palette + image + ending)
    assert valid_png_capture(file)


def test_png_rejects_misordered_critical_chunks(tmp_path):
    import struct
    import zlib
    from tools.verify_windows import valid_png_capture

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 100, 100, 8, 2, 0, 0, 0))
    encoded = zlib.compress((b"\x00" + b"\x20\x30\x40" * 100) * 100)
    image = chunk(b"IDAT", encoded)
    ending = chunk(b"IEND", b"")
    prefix = b"\x89PNG\r\n\x1a\n"
    file = tmp_path / "png.png"
    for payload in (
        prefix + header + header + image + ending,
        prefix + header + image + chunk(b"PLTE", b"\x00\x00\x00") + ending,
        prefix + header + image + chunk(b"tEXt", b"x\x00y") + image + ending,
        prefix + header + image + chunk(b"FAKE", b"abc") + ending,
    ):
        file.write_bytes(payload)
        assert not valid_png_capture(file)
