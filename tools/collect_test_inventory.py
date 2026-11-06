"""Independent pytest collection inventory for Windows QA.

An automated PASS requires every collected test to occur exactly once in JUnit;
minimum counts and one passing testcase per Qt module are insufficient.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def case_identity(nodeid):
    parts = nodeid.split("::")
    if len(parts) < 2 or not parts[0].endswith(".py"):
        raise ValueError("Invalid pytest test identity")
    module = parts[0][:-3].replace("/", ".").replace("\\", ".")
    return [".".join([module, *parts[1:-1]]), parts[-1]]


class InventoryCollector:
    def __init__(self):
        self.cases = None

    def pytest_collection_finish(self, session):
        self.cases = [case_identity(item.nodeid) for item in session.items]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args, extra = parser.parse_known_args(argv)
    if args.output.exists():
        raise RuntimeError("Refusing to overwrite a prior test inventory")
    import pytest
    collector = InventoryCollector()
    # Always collect from the same project/test root and with the same
    # third-party plugin isolation as the actual regression run.
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    status = int(pytest.main(["--collect-only", "-q", "tests", *extra], plugins=[collector]))
    if status != 0 or not collector.cases or len(set(map(tuple, collector.cases))) != len(collector.cases):
        raise RuntimeError("Test collection failed or contains duplicate identities")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps({"schema": 1, "cases": collector.cases}, indent=2), encoding="utf-8")
    tmp.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
