# BCC cloud control — pilot, NOT RELEASE

This controller is based on existing BCC Kie.ai offline handoff and budget code. It does **not** merge the diverged PR #12 source. GitHub Codespaces is a temporary 2-core Linux workspace, not a continuously running agent. The Windows PC workspace is `G:\\BCC`, reserved for native Qt acceptance, not cloud development.

## First cloud session (manual activation; check GitHub quota first)

1. In GitHub open the BCC repository and select branch `agent/bcc-cloud-control-20261122`.
2. Use **Code → Codespaces → Create codespace on this branch**; choose 2 cores. Check remaining included Codespaces usage and configure no additional billable spending before creation. Running and storing a Codespace consumes GitHub quota.
3. In the cloud terminal run:

   ```bash
   python -m unittest discover -s tests -p test_bcc_cloud_control.py -v
   python tools/bcc_cloud_control.py preflight
   ```

4. Create a separate detached snapshot of PR #12:

   ```bash
   git fetch origin codex/hub-demand-recovery-20261118
   git worktree add --detach /tmp/bcc-pr12 FETCH_HEAD
   git -C /tmp/bcc-pr12 rev-parse HEAD
   ```

   Compare the exact SHA with the current head of GitHub PR #12. The controller refuses an unpinned task; do not follow a moving branch without re-review.

5. Make an immutable, read-only task proposal (replace the placeholder with the checked SHA):

   ```bash
   python tools/bcc_cloud_control.py plan --target-sha <exact-40-character-sha>
   ```

6. Optional Graphify code-only analysis *in the cloud*, without a local model. Install the official PyPI distribution **graphifyy** with `uv tool install graphifyy` as in Graphify documentation, after reviewing available quota. Then run:

   ```bash
   GRAPHIFY_BACKEND=none graphify extract /tmp/bcc-pr12 --code-only --backend none
   GRAPHIFY_BACKEND=none graphify query "how does market data reach risk and paper entry?"
   ```

   Use code-only and backend=none. Default extraction of docs/media may invoke an API backend if credentials are present. Never commit `graphify-out`.

7. Stop the Codespace to avoid wasting included compute. Do not launch GitHub Actions or start the working trading program.

## Security and cost gates

- The cloud preflight and task proposal have **zero paid API network paths**. Graphify CLI, if installed, is launched only in code-only/backend-none mode, with inherited provider keys removed by the controller's own `graph` command. The manual Graphify commands above also specify backend none explicitly.
- GPT Work and local Ollama/Qwen are out of scope. GPT via the **OpenAI API** remains the planned controller model, but no autonomous API transport has been enabled here.
- Existing Kie.ai budget accounting and unresolved old spending holds must be reconciled before any paid request. A new Codespace-local SQLite file cannot be considered a fresh $20 budget.
- OpenAI API and Codespaces require **separate** budgets from the Kie.ai $20/month ceiling. The user's architecture approval is **not** paid-call approval.
- No trading credentials, bot databases, logs, tokens, .env or user data in GitHub, prompts, graph files or PR comments. The repository is public by user choice.
- No workflow triggers, no merge/release, no autonomous GitHub writes, no real order execution. Manual Windows clean-unpack Qt E2E remains mandatory.

## Further engineering required

This is an **offline cloud control scaffold**, not an autonomous agent. Before enabling GPT/Kie.ai calls or branch writes: implement independently verifiable approval identity and persistent shared billing admission, reconcile old holds, enforce per-call budgets at transport, restrict GitHub scopes to a staging branch, implement safe tool execution sandbox and audit logs, and verify refusal behavior under faults. The Kie.ai gateway and PR #12 have diverged; do not merge blindly. The BCC release remains blocked until full-source regression, real worker/risk PAPER admission recovery and native Windows Qt E2E are proven.

## Linux Qt system dependency for regression (2026-11-22)

On an already-running Codespace created before the new Dockerfile, tests may fail during collection with `ImportError: libGL.so.1`. This is a **container system-library failure**, not a BCC test failure. Fix the existing container without a rebuild (which would remove `/tmp/bcc-pr12`):

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  libgl1 libegl1 libopengl0 libglib2.0-0 libdbus-1-3 libfontconfig1 \
  libxkbcommon-x11-0 libxcb-cursor0 libxcb-xinerama0 libxcb-icccm4 \
  libxcb-image0 libxcb-keysyms1 libxcb-render-util0
cd /tmp/bcc-pr12
QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=.:tests \
  /tmp/bcc-full-qa/bin/python -m pytest -q tests > /tmp/bcc-full-qa.log 2>&1
result=$?
tail -n 35 /tmp/bcc-full-qa.log
printf 'EXIT_CODE=%s\\n' "$result"
```

The Codespaces Dockerfile now records these dependencies for subsequent rebuilds. A code diff and mock tests do not verify the Docker build; verify in the actual Codespace. The source under test must remain at the reviewed PR #12 SHA. Do not install desktop/VNC drivers or touch live Windows bots.

## PR #12 full Linux regression and offline GPT review packet

The user's Codespaces full test suite on pinned source `12816756c13f9e979db26c9ffa01b4be8374c9a8` completed **4018 passed, EXIT_CODE=0** on CPython 3.12.16 and headless PySide6. Evidence and Windows limitations: `docs/BCC_PR12_LINUX_QA_20261122.md`.

The next cloud controller phase is a **source-bound offline GPT review packet**. It does not send model requests, query tokens or modify branches. In the existing Codespace, switch to the controller branch before running it:

```bash
cd /workspaces/Bot-Control-Center-Pro
git pull --ff-only origin agent/bcc-cloud-control-20261122
python -m unittest discover -s tests -p 'test_bcc_openai_job.py' -v
python -m tools.bcc_openai_job \
  --root /tmp/bcc-pr12 \
  --sha 12816756c13f9e979db26c9ffa01b4be8374c9a8
```

Expected report is `OFFLINE_REVIEW_BUNDLE`, `approval=NOT_GRANTED`, `network_calls=DISABLED`, `paid_calls=DISABLED`. The optional `--output /tmp/bcc-reviewed-task.txt` generates review text outside the public source repository with restrictive file permissions; this file is not a credential or a paid-call approval. Source excerpts are restricted to five reviewed market-data paths and original source hashes; an unreviewed/new SHA, modified tracked source, checkout on a branch, symlink, oversize text, or potential embedded token aborts generation.

The source ranges were checked against the source-pinned GitHub files (approximately 54.8 KB of excerpts, below 90 KB total packet ceiling). This is a **static review bundle, not a full autonomous GPT agent**. Actual provider use still requires an approved model and maximum price, an auditable one-task authorization bound to the exact prompt hash, a persistent ledger distinct from the historical Kie.ai holds, credential isolation, and an independently enforced billable transport. The existing Kie.ai $20/month ceiling cannot be reused as OpenAI credits. PR #14 remains draft; no live trading, automatic writes, provider calls or release until separate review and explicit consent.

## OpenAI API cost controls — offline simulation only

The connected Codespace has verified the SHA-bound review task (`input_sha256=962cb7375a37f877c5932277ae94a6fe83ed2ebfe00e7611609b1e4bab797092`; 55,515 bytes) with **9/9 source-bundle tests PASS**. This was **not** a provider request or model response. Source snapshot remains `12816756c13f9e979db26c9ffa01b4be8374c9a8`.

A separate `tools/bcc_openai_budget.py` now implements an **offline spending simulation**:
- no default OpenAI monthly budget: simulation initialization requires an explicit positive cap;
- request ID, one-time test approval ID, selected model, target commit, prompt SHA-256 and maximum reservation amount are bound together;
- SQLite transaction is atomic; repeated approval/request IDs fail closed; pending holds consume capacity across month boundaries;
- a missing/unreadable/corrupt ledger blocks all operations;
- verified invoice settlement and actual HTTP transport are **not implemented** (unresolved holds remain occupied);
- Kie.ai's separate $20 limit is left unchanged; a new local Codespace ledger is not proof of available OpenAI funds.

Run **new** offline tests in the current cloud controller checkout after pulling PR #14:

```bash
cd /workspaces/Bot-Control-Center-Pro
git pull --ff-only origin agent/bcc-cloud-control-20261122
python -m unittest discover -s tests -p 'test_bcc_openai_budget.py' -v
python -m unittest discover -s tests -p 'test_bcc_openai_job.py' -v
```

To create an offline prompt file **in the temporary workspace only**, and not accidentally publish source excerpts to GitHub:

```bash
python -m tools.bcc_openai_job \\
  --root /tmp/bcc-pr12 \\
  --sha 12816756c13f9e979db26c9ffa01b4be8374c9a8 \\
  --output /tmp/bcc-reviewed-task.txt
```

**This is not API integration.** Test verifier callbacks are synthetic and must never be treated as user authorization. Before any real OpenAI call: fix the actual model and verified price, explicitly approve a dollar cap for that exact task and prompt, persist and reconcile a trusted accounting ledger across Codespace lifecycles, build a network transport that independently enforces the approval and spending ceiling, and verify secrets/retries and GitHub write permissions. Neither project approval nor free trial/account credits are consent to spend. No Work, Qwen/Ollama, trading keys or bot restarts.
