"""Modern Qt application entry point."""

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from bcc.ui import MainWindow, apply_style
from bcc.runtime_paths import data_root
from bcc.bootstrap import ensure_runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-root", type=Path, default=None
    )
    args = parser.parse_args()
    root = data_root(args.data_root)
    ensure_runtime(root)
    app = QApplication(sys.argv)
    app.setApplicationName("Bot Control Center Pro")
    window = MainWindow(root)
    apply_style(app, window.preferences)
    window.show()
    result = app.exec()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return result


if __name__ == "__main__":
    sys.exit(main())
