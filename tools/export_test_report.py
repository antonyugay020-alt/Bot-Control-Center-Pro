"""One-command / after-close PAPER diagnostic ZIP from TEST_DATA.

Never stops workers, trades, closes positions, or copies unrelated bot roots.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from bcc.support import export_current_report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--ui-exit-code", type=int, default=None)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "MY_BOTS").mkdir(parents=True, exist_ok=True)
    out = root / "REPORTS"
    out.mkdir(exist_ok=True)
    filename = "BCC_TEST_REPORT_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6] + ".zip"
    path = out / filename
    try:
        summary = export_current_report(
            root, path, allow_empty=True,
            session_info={"ui_exit_code": args.ui_exit_code,
                          "automatic_after_exit": args.ui_exit_code is not None},
        )
    except Exception as exc:
        print("[REPORT ERROR] " + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 1
    print("REPORT: " + str(path))
    print("REPORT BOTS: " + str(summary["bot_count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
