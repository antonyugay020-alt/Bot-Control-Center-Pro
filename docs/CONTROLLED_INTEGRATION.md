# Controlled Integration and Acceptance Protocol

Approved 2026-11-07. This protocol controls development, not a claim of acceptance.

1. At session start fetch HEAD, open PR, prior evidence, blockers and unfinished work. Detect concurrent commits and avoid overwrites. Continue existing integration branch, not main.
2. Work in coherent, end-to-end architectural milestones across interfaces, storage, lifecycle, error handling and UI. Fix systemic root causes together, not one symptom per cycle. Prefer code integration over reports.
3. Implement source and regression tests. Perform feasible unit, integration, concurrency, recovery and regression tests. Record command, platform, baseline SHA, tests and failures accurately. Inability to run a check is not PASS.
4. Commit changes and an evidence-based durable development checkpoint only after verifying actual modifications. Next run resumes from checkpoint; no blind repeats or duplicated branches.
5. Keep an independent defect inventory, group failures by root cause and rerun affected regression after changes. Reuse test evidence only when matching source and environment.
6. Main/merge/release are blocked until required project-specific whole-system regression, independent review and clean-unpack Windows GUI E2E acceptance. No interim release, unverified claims or artificial success counters.
7. No secrets in repository, unauthorized trading orders, unapproved API spending or costly GitHub Actions. Block safely on unclear authorization and unsupported capability.
8. User communication: only significant verified milestones and genuine blockers needing user action. Do not send routine progress dumps.

Checkpoint template: baseline commit; resulting commit; completed capability and integration edges; exact tested commands/results; regressions and open blockers; pending Windows E2E; precise next milestone.
