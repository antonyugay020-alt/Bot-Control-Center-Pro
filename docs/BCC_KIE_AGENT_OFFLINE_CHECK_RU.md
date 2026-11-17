# BCC — Claude Sonnet 5.5 через Kie.ai: локальная проверка без расходов

СТАТУС: РАЗРАБОТКА. Реальный Claude не подключён, платные запросы отключены.
Документ относится только к отдельной ветке агента BCC:
agent/bcc-work-claude-gateway-20261117.

## Запуск на Windows / PowerShell 5.1

Использовать ранее созданную папку BCC_AGENT_GATEWAY; не запускать команды
в каталоге с работающими ботами.

    $dir = "$HOME\Desktop\BCC_AGENT_GATEWAY"
    $want = "agent/bcc-work-claude-gateway-20261117"
    if (-not (Test-Path "$dir\.git")) { throw "Нет проверенной папки BCC_AGENT_GATEWAY" }
    if ((git -C $dir branch --show-current).Trim() -ne $want) { throw "Неверная ветка" }
    if (git -C $dir status --porcelain) { throw "Есть локальные изменения. Не обновляю." }
    git -C $dir pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "Git update failed" }
    & "$dir\BCC_KIE_AGENT_CHECK.cmd"

One-click offline script invokes these commands only:

    py -3 -I "$dir\tools\bcc_kie_agent.py" doctor
    py -3 -I "$dir\tools\bcc_kie_agent.py" mock

doctor: reads whether Windows Credential Manager contains the BCC Kie.ai
credential, then tries a LOCAL readback. It discloses only presence and
readability statuses, never the key value, length or hash. A result
local_readback=READABLE_VALUE_HIDDEN confirms local accessibility, not
acceptance by the Kie.ai server.

mock: checks handling of synthetic Anthropic-like and OpenAI-like
reply shapes with fake token usage. It does not access the provider.
mock_contract=PASS is NOT a real compatibility confirmation.
Model ID, endpoint, streaming, tool calling and provider receipts remain
unverified. The module has no HTTP client and paid_request always denies.

Neither command touches live bots, trading positions, databases, exchange
credentials, Windows services, GitHub Actions or Graphify.

## Optional offline price quote

For an ordinary local task text file without secrets:

    py -3 -I "$dir\tools\bcc_kie_agent.py" quote --task-file "C:\path\TASK.txt"

This prints only a SHA-256 of the task, token ceilings and a preliminary
upper quote using the 2026-11-17 user-supplied screenshot prices.
No transmission, reservations or charges occur. Do not commit local task
files to GitHub or include any operator secrets.

## Financial and safety rules

- One shared Kie.ai monthly ceiling: $20. Target admission limit: $18;
  $2 safety reserve. An amount-based limit is NOT standing permission.
- Explicit separate user confirmation is mandatory before EVERY future
  paid API call, even one costing less than $0.01.
- tools/bcc_claude_budget.py is an offline SQLite policy prototype, not a
  verified provider-side billing guarantee. It is not yet wired to Kie.ai.
- No live transport until official endpoint, model ID, usage schema,
  pricing, reconciliation, and protected approval boundary are verified.
- The existing key is local to the Windows user. GPT Work cloud cannot
  directly read Windows Credential Manager. Work, Graphify and Kie.ai
  are not yet directly interconnected.
- BCC live bots, monitor, databases and keys must remain untouched.
  Windows reboot and unapproved GitHub Actions are prohibited.

## Offline evidence

Python 3/Linux: 12 isolated unit tests passed with no failures.
The synthetic mock probe passed with paid_calls=DISABLED.
This is not a complete BCC regression, native Windows Qt E2E, real Kie.ai
compatibility test or release acceptance.

Next gate: read back the saved key LOCALLY on Windows using doctor
without disclosing it, then independently validate public Kie.ai API
documentation, with no paid requests.
