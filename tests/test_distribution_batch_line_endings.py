"""All distributed Windows entrypoints must use CRLF, not mixed/LF-only lines."""
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("name", [
    "START.bat", "INSTALL.bat", "VALIDATE_WINDOWS.bat",
    "CHECK_WINDOWS.bat", "TEST_START.bat", "REPORT_NOW.bat",
])
def test_distributed_windows_batch_uses_crlf(name):
    content = (ROOT / name).read_bytes()
    assert b"\r\n" in content
    assert b"\n" not in content.replace(b"\r\n", b"")
