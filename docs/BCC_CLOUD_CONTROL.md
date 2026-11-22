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
   GRAPHIFY_BACKEND=none graphify extract /tmp/bcc-pr12 --code-only
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

## Pre-application patch review gate (2026-11-22)

The read-only patch checker `tools/bcc_patch_gate.py` examines a candidate unified Git diff before a human reviewer authorizes any source changes. It accepts *only* changed files in the explicitly reviewed Bybit market-data / regression-test allowlist, and requires the exact PR #12 commit and prompt hash. It refuses new, deleted, renamed, symlink and binary files; unknown paths; duplicate file patches; obvious secrets; structurally unusual patches; stale hunks; and changes above 1,000 lines. It runs **only** `git apply --check`, `--numstat` and `--summary` (never bare `git apply`). A passing report is NOT approval, not actual patch application, and not a test result.

Locally constructed offline tests exercised the patch gate's Git checks in an isolated mock-source repository; **10/10 passed**, but the dependent source-validation function was stubbed for this local test. The actual PR14 integration with the pinned PR12 snapshot must be rechecked in Codespaces.

When PR #14 is pulled, verify all its no-cost controls in Codespaces:

```bash
cd /workspaces/Bot-Control-Center-Pro
git pull --ff-only origin agent/bcc-cloud-control-20261122
python -m unittest discover -s tests -p 'test_bcc_cloud_control.py' -v
python -m unittest discover -s tests -p 'test_bcc_openai_job.py' -v
python -m unittest discover -s tests -p 'test_bcc_openai_budget.py' -v
python -m unittest discover -s tests -p 'test_bcc_patch_gate.py' -v
```

A candidate patch generated and separately reviewed for the pinned task could be checked **without applying it**, but none has been generated or authorized yet:

```bash
python -m tools.bcc_patch_gate \\
  --root /tmp/bcc-pr12 \\
  --sha 12816756c13f9e979db26c9ffa01b4be8374c9a8 \\
  --prompt-sha256 962cb7375a37f877c5932277ae94a6fe83ed2ebfe00e7611609b1e4bab797092 \\
  --diff /tmp/reviewed-candidate.diff
```

Do not invent or create a dummy patch solely to force this check. This module implements **no** model invocation, no automatic patch application, no GitHub writes and no merged release. Windows Qt E2E and financial protection checks still gate acceptance.

## Local candidate branch staging (2026-11-22; no PR12 modification)

`tools/bcc_candidate_stage.py` is a strictly local, review-only staging boundary. It takes an **already proposed** unified patch, re-verifies the exact PR12 commit and GPT task digest, runs `tools/bcc_patch_gate.py` again, and creates a **new local Git worktree and branch** named `agent/bcc-pr12-review-<diff-digest>` in a private temporary folder. It disables Git hooks and inherited Git provider/config environment and refuses local Git checkout filters. It performs a second `git apply --check`, applies the patch to the **new** worktree only, checks Git status, changed line counts, and baseline SHA, and verifies that the original detached source snapshot is unchanged. If staging fails, it attempts to remove only the newly created stage and candidate branch.

It **does not** execute candidate code, run tests, commit, push, create GitHub PRs, merge, contact models or place orders. A passing stage is a **proposal for manual review**, not a security sandbox or proof that the patched source is safe. The working tree shares the local repository's Git object store; it is isolated for edits, not a hostile-code execution boundary.

Local isolated Git fixture tests exercised 10 scenarios successfully; the true-source and patch-gate interfaces need confirmation in Codespaces. The user had already confirmed **42/42 PASS** for the previous cloud controller guards.

Check the new staging integration without staging an actual unreviewed patch:

```bash
cd /workspaces/Bot-Control-Center-Pro
git pull --ff-only origin agent/bcc-cloud-control-20261122
python -m unittest discover -s tests -p 'test_bcc_candidate_stage.py' -v
```

If the final task eventually receives a reviewable `/tmp/bcc-reviewed-candidate.diff`, after inspecting the patch text, the explicit **local-only** staging command is:

```bash
python -m tools.bcc_candidate_stage \\
  --root /tmp/bcc-pr12 \\
  --sha 12816756c13f9e979db26c9ffa01b4be8374c9a8 \\
  --prompt-sha256 962cb7375a37f877c5932277ae94a6fe83ed2ebfe00e7611609b1e4bab797092 \\
  --diff /tmp/bcc-reviewed-candidate.diff
```

No such candidate diff has yet been produced. Do not fabricate one just to exercise the staging action. `/tmp` worktrees are ephemeral: save reviewed output according to the release workflow before destroying a Codespace. GitHub publication and any subsequent execution remain separately blocked. An independently reviewed test runner in a container with no credentials/network will be needed before executing model-written Python, even when the patch has passed this structural gate.

## Next candidate boundary: Linux Docker QA, no network or host fallback

At the 2026-11-22 checkpoint the user verified **10/10 PASS** for local staging, completing **52/52 PASS** across the five controller test groups (10 control + 10 GPT bundle + 12 budget + 10 patch gate + 10 local staging). The **4,018/4,018** passing core tests were on the untouched PR #12 base and are not a test of any future AI-generated patch.

`tools/bcc_candidate_qa.py` is a separate **deny-by-default** test execution boundary. It accepts an already-inspected patch and previously staged temporary branch. Before any execution, it validates the exact source commit and prompt digest, patch hash, allowed file list, candidate branch name, local Git common dir, unmodified source, and candidate status.

The default command only emits `OFFLINE_DOCKER_QA_PLAN`. Only the explicit `--execute-offline` flag may run full Linux tests in a locally cached Docker image pinned to its **exact image ID** (`sha256:...`). It will not pull an image or silently execute on the host if Docker is unavailable. The container has `--network=none`, no Linux capabilities, `no-new-privileges`, read-only source and root filesystem, fixed temporary scratch, unprivileged user, 2 CPUs, 3 GB memory, 256 PID limit, and 15-minute maximum; test output is size-limited and stored in a new private temporary log. There is no model, GitHub writer or Windows/trading integration.

A *separate* QA image recipe is available at `tools/qa/BCC_QA_Dockerfile`. Building this image is a manual, networked package installation and can consume Codespaces quota. The built image must be reviewed and its exact local ID checked. The runtime itself never builds/pulls the image and has no network.

New offline tests `tests/test_bcc_candidate_qa.py` check the Docker argv and scope/identity preflight through mocks; **these have not yet run in Codespaces**, and no actual candidate patch exists. Verify the new safety gate first:

```bash
cd /workspaces/Bot-Control-Center-Pro
git pull --ff-only origin agent/bcc-cloud-control-20261122
python -m unittest discover -s tests -p 'test_bcc_candidate_stage.py' -v
python -m unittest discover -s tests -p 'test_bcc_candidate_qa.py' -v
```

Do **not** build a Docker image or run candidate QA yet: there is no accepted candidate patch, and a passing synthetic test cannot substitute for patch review. When an actual patch is proposed, its diff and source identity must first pass `bcc_patch_gate.py`, be manually reviewed, and be staged using `bcc_candidate_stage.py`; only then should the operator consider `bcc_candidate_qa.py` execution.

Linux Docker isolation is a defense-in-depth control, not a guarantee against container runtime escapes or malicious code. The Docker image may need adjustments for project tests, and this approach **does not replace** Windows clean-unpack Modern Qt E2E or prove live Bybit market-data recovery. No paid provider calls, live trades, GitHub Action runs, automatic patch publication, branch merges, or release decisions are authorized.
