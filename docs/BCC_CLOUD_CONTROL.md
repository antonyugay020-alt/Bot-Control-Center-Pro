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
