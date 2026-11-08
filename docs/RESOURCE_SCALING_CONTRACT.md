# Resource-based bot and worker scaling

The BCC fleet must not depend on a baked-in target or maximum number of bots,
accounts, strategies, threads or operating-system worker processes.

- A bot identity is a durable accounting/control owner, not a thread slot.
- Registration and saved MY_BOTS inventory must accept arbitrary counts,
  subject to disk, memory, OS, provider and other physical resource availability.
- Thread/process concurrency is an operator-adjustable resource budget, not
  a bound on registered bot identities. Never reject creation because a
  preselected throughput number has been reached.
- Separate scheduling admission/backpressure, provider quotas, API limits and
  optional user-defined risk limits from fleet cardinality. When a real
  resource limit blocks execution, return the exact limiting reason and
  preserve desired-state commands for later reconciliation.
- An unset dispatch queue budget means no artificial software queue ceiling.
  Explicit finite per-workload queue budgets remain available to prevent
  memory exhaustion without becoming fleet-size restrictions.
- Shared-risk maximum positions 0 means unrestricted by that optional policy.
  Positive values are user-chosen, not limited by an arbitrary application
  constant. Financial/margin safety checks still apply.
- Test workloads use varied owner and thread counts to verify invariants.
  Fixture sizes do not define supported fleet targets or ceilings.

Full clean-unpack Windows Qt E2E and resource-exhaustion recovery remain
required for release certification.
