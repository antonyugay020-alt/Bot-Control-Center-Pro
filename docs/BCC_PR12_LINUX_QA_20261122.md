# BCC PR #12 — source-pinned Linux regression evidence (2026-11-22)

User-supplied Codespaces terminal transcript; not independently re-executed by this PR.

- Repository: `antonyugay020-alt/Bot-Control-Center-Pro`
- Source under test: `12816756c13f9e979db26c9ffa01b4be8374c9a8` (detached PR #12 worktree `/tmp/bcc-pr12`)
- Environment: GitHub Codespaces Linux, CPython 3.12.16, PySide6 6.12.0, pytest 9.2.0; Qt platform `offscreen`; plugin autoload disabled; `PYTHONPATH=.:tests`
- Command: `QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=.:tests /tmp/bcc-full-qa/bin/python -m pytest -q tests`
- Result: **4018 passed in 338.79s (0:05:38), EXIT_CODE=0**; no failures or skips in reported summary.
- Earlier focused regression: **33 passed in 9.42s** against 4 recovery-related test files on Python 3.14.
- Earlier first full regression attempt: collection blocked by missing `libGL.so.1` in Codespaces; installing Qt Linux system libraries resolved this; `QApplication` probe printed `QT_READY`.

## What this verifies — and does not

This is strong evidence the available Linux test suite passes on the pinned PR #12 source and that the cloud Qt dependencies are sufficient for headless Qt tests. It **does not prove** that the PR #12 Windows Qt acceptance happened, that Bybit external history timeouts have stopped, that all exchange data is continuously fresh, or that financial safeguards can be relaxed. **NOT RELEASE**.

The cloud controller work is isolated in PR #14, whose base is the earlier gateway branch rather than PR #12; these branches have diverged. No merge, bot restart, live order, GitHub Action, or paid API operation was performed by this evidence update.
