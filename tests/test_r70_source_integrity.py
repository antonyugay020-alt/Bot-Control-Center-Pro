"""Tampering with a launcher or a linked ancestor must block QA."""
import hashlib
import json
import os
from pathlib import Path

import pytest
from tools.verify_package import REQUIRED_FILES, SourceIntegrityError, verify


def test_missing_start_script_cannot_be_omitted_from_manifest(sealed_r70):
    root = sealed_r70
    (root / "START.bat").unlink()
    manifest = json.loads((root / "SHA256_MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"].pop("START.bat")
    (root / "SHA256_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SourceIntegrityError, match="обязательный"):
        verify(root)


def test_missing_core_engine_cannot_be_omitted_from_manifest(sealed_r70):
    root = sealed_r70
    (root / "bcc/trading.py").unlink()
    manifest = json.loads((root / "SHA256_MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"].pop("bcc/trading.py")
    (root / "SHA256_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SourceIntegrityError, match="обязательный"):
        verify(root)


def test_linked_directory_inside_package_is_rejected(sealed_r70):
    root = sealed_r70
    alias = root / "bcc" / "alias"
    target = root / "bcc" / "src"
    target.mkdir()
    (target / "extra.py").write_text("x = 1", encoding="utf-8")
    try:
        alias.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not available")
    manifest = json.loads((root / "SHA256_MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"]["bcc/alias/extra.py"] = hashlib.sha256(
        (target / "extra.py").read_bytes()).hexdigest()
    manifest["files"]["bcc/src/extra.py"] = manifest["files"]["bcc/alias/extra.py"]
    (root / "SHA256_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SourceIntegrityError, match="Ссылка"):
        verify(root)


@pytest.fixture
def sealed_r70(tmp_path):
    root = tmp_path / "sealed"
    root.mkdir()
    hashes = {}
    for name in (*REQUIRED_FILES, *(f"tests/test_{i}.py" for i in range(30))):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = name.encode()
        path.write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    (root / "SHA256_MANIFEST.json").write_text(json.dumps({
        "checkpoint": "FIXTURE", "files": hashes
    }), encoding="utf-8")
    assert verify(root)["status"] == "PASS"
    return root
