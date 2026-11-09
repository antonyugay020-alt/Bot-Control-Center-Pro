"""Native Windows Qt pytest lifetime: keep QApplication alive through teardown.

C++ Qt objects, especially thread-local objects owned by QThreadPool, must be
released before Python/Shiboken interpreter teardown. This entry point is used
ONLY for the full native Qt test shard. It never suppresses pytest failures or
an abnormal process exit, and never touches user bot processes or state.
"""
import sys


def main(args=None):
    import pytest
    from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    # Strong reference remains live until after every native Qt test, pending
    # deletion, and the QThreadPool global workers have stopped.
    result = 1
    cleanup_ok = True
    try:
        result = int(pytest.main(sys.argv[1:] if args is None else args))
    finally:
        try:
            for widget in app.topLevelWidgets():
                if hasattr(widget, 'wait_for_snapshot_shutdown'):
                    if not widget.wait_for_snapshot_shutdown(30000):
                        cleanup_ok = False
                        print('Qt shutdown: monitor snapshot pool not quiescent', file=sys.stderr)
                widget.close()
                widget.deleteLater()
            pool = QThreadPool.globalInstance()
            if not pool.waitForDone(30000):
                cleanup_ok = False
                print('Qt shutdown: global QThreadPool still active', file=sys.stderr)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        except Exception as exc:
            cleanup_ok = False
            print(f'Qt shutdown failure: {type(exc).__name__}: {exc}', file=sys.stderr)
    return result if cleanup_ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
