# BCC: одноразовый тест GPT-6.1 Terra через Kie.ai

Дата: 18.11.2026. Изолированная ветка agent/bcc-work-claude-gateway-20261117.
Статус: код подготовлен, новая Windows-приёмка ещё не проведена.

## Назначение

Ровно один простой текстовый запрос Terra с существующим ключом из
Windows Credential Manager. Не агентный запуск, не передача кода BCC,
не торговля, не GitHub Actions и не вызовы другой модели.

Endpoint POST https://api.kie.ai/codex/v1/responses; model=gpt-6-1-terra;
stream=false; max_output_tokens=256; без tools, web search и файлов.
Тестовый prompt: Reply with exactly BCC_TERRA_CONNECTED and no other text.

## Разрешение и расходы

Один запрос возможен только после новой успешной офлайн-приёмки
(16 тестовых модулей, минимум 218 проверок) и ввода в интерактивный
Windows-терминал точной строки:

    APPROVE TERRA 0.01

$0.01 — ЛОКАЛЬНЫЙ резерв, а НЕ гарантированный провайдерский потолок.
Прайс Terra в письме Kie.ai и на странице Pricing расходится;
минимальные сборы и реальный биллинг не подтверждены. Если необходим
абсолютный лимит $0.01 у провайдера — отменить ввод подтверждения
до получения серверной гарантии. Общий бюджет Kie: $20 в месяц,
допуск $18, резерв $2. Это не разрешение на другие покупки.

## Архитектура

Программа tools/bcc_kie_terra_one_shot.py читает ключ только локально
по target BCC.DevAgent.KieAI.APIKey.v1 после подтверждения.
Перед HTTPS она атомарно создаёт запись HOLD $0.01 в общем журнале
%LOCALAPPDATA%\BCCDevAgent\claude_budget.sqlite3, вместе с прежним
незавершённым Claude HOLD. Новое разрешение связано с одним model=terra,
фиксированным source SHA и SHA-256 фиксированного prompt.
Дополнительный маркер потреблённой попытки:
%LOCALAPPDATA%\BCCDevAgent\terra_one_shot_20261118_v1.attempted.
Запрещено очищать эти записи ради повторного вызова.

Запрос: один HTTPS POST на фиксированный host/path. Без retry,
redirect, fallback, tools и сырого вывода error body. При ошибке,
таймауте или неверном ответе повторов нет, HOLD сохраняется.
Никакая функция шлюза не сверяет счёт только по данным usage.
В консоль выводятся код HTTP, количество токенов, SHA-256 ответа и
статус; никогда не печатаются API-ключ и сырой текст ответа.

## Windows запуск

В уже существующей папке BCC_AGENT_GATEWAY:

    $dir = "$HOME\Desktop\BCC_AGENT_GATEWAY"
    if (-not (Test-Path "$dir\.git")) { throw "Нет отдельной папки BCC" }
    git -C $dir pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "Ошибка GitHub" }
    & "$dir\BCC_KIE_TERRA_ONE_TEST.cmd"
    Write-Host "Код завершения: $LASTEXITCODE"

Сначала скрипт выполняет полную офлайн-регрессию. Только при PASS
запускает интерактивное подтверждение для одного нового запроса.
Папка QA остаётся прежней: BCC_AGENT_GATEWAY\QA\BCC_KIE_QA_RESULT.zip.
Никаких изменений действующих BCC-ботов, мониторов или торговых баз.

## Ограничения результата

CONNECTED означает только ответ от Terra на один тестовый prompt.
Это не E2E автономного программиста и не полный Windows Modern Qt E2E BCC.
После запроса требуется сверить Kie.ai Logs/Usage и фактическое списание.
Старый Claude one-shot навсегда закрыт, Kimi/Astra не запускаются.
Новый source SHA и пакет из 218 тестов ещё должны быть проверены Windows.

## INCIDENT 2026-11-18 — TERRA AUTHORIZATION CONSUMED / OUTCOME UNKNOWN

User provided Windows PowerShell transcript of the ONE authorized test at
source SHA 56d1a87c9171d7e257a9f19881d6a72b8944e9c7:

- Windows offline: Ran 218 tests in 1.388s, OK (skipped=2), exit 0.
- User typed APPROVE TERRA 0.01 in local interactive terminal.
- Python returned BLOCKED/UNKNOWN, no JSON SafeOutcome, launcher exit 1.
- There is NO evidence in that transcript that Kie.ai received the
  Terra request or debited any credits.
- A received non-200 HTTP response in this client would normally produce
  a sanitized JSON result HTTP_ERROR_NO_RETRY. Because no JSON appeared,
  an exception occurred before a SafeOutcome was returned; this can
  happen before HTTPS or during a connection/response exception.
  Exact stage is unknown until inspecting the local one-shot state.

Critical: The sole Terra approval is CONSUMED. Do not rerun the paid
script or delete/move/settle the old HOLD or attempt marker to enable
a retry. No new paid requests have been authorized.

New code on the same isolated branch:
- tools/bcc_kie_terra_readonly_diagnose.py: SQLite mode=ro query and
  marker-existence check ONLY, no credentials, no network, no writes.
- BCC_KIE_TERRA_STATUS.cmd: read-only Windows entrypoint.
- BCC_KIE_TERRA_ONE_TEST.cmd: RETIRED; now read-only status only.
- tools/bcc_kie_terra_one_shot.py: known consumed authorization hard
  block in main and in the production send path.

Stage interpretation:
- NO_TERRA_HOLD / NO_LEDGER: no local Terra reservation; failure before
  durable reservation, but provider delivery still needs external logs.
- HOLD_BEFORE_SENTINEL: reserved amount, marker not written; program
  did not reach its usual network send branch.
- ATTEMPT_GATE_REACHED_HTTP_UNKNOWN: hold + marker exist; it reached
  the pre-network gate, but a provider HTTP transaction is NOT proven.
- UNKNOWN_* / inconsistent marker/ledger: fail closed and preserve evidence.

Read-only Windows command after updating this same isolated checkout:

    $dir = "$HOME\Desktop\BCC_AGENT_GATEWAY"
    git -C $dir pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "GitHub update failed" }
    & "$dir\BCC_KIE_TERRA_STATUS.cmd"

Paste only its sanitized JSON status. Do not share API keys, full ledger,
private trading data, or credentials. Kie.ai Logs/Usage for the
corresponding timestamp are needed to confirm provider-side receipt
and actual billing. Even a zero-credit entry does not itself make
a new request approved.

Previous instructions above describe the historical test. They must
NOT be used to authorize or trigger another request as of this incident.

## PROVIDER LOG CONFIRMATION — 2026-11-18 14:27:03 (Kie.ai dashboard)

User provided a screenshot of Kie.ai Logs containing one new row:
- model label `gpt-6-1-terra`;
- timestamp `2026-11-18 14:27:03` shown in Kie.ai UI;
- provider status green `успешно` (success);
- duration `9` shown in UI (unit not independently audited);
- credits used `0.01` as shown by Kie.ai (NOT a verified USD amount).

The previous Claude row remains an error with zero credits used.
Therefore the earlier statement that provider delivery was unproven
is superseded: **provider-side completion is now supported by the log.**
However the local Windows one-shot printed `BLOCKED/UNKNOWN` with
exit 1 and did not produce a JSON SafeOutcome. This is a mismatch
between provider success and client acceptance/exception handling.

Do NOT assert the parser is conclusively at fault: the screenshot
does not display the raw provider response envelope, usage object,
actual terminal exception, or exact SDK error. A successful provider
log row cannot prove the client received a complete accepted body.
Possible locations include HTTPS response read/decode and strict
local Responses shape/usage validation. Need the corresponding result
JSON (redacted) or Kie.ai dashboard Result view to determine cause.

Provider credits and the local `$0.01` reservation are different
measurements. The existing SQLite HOLD/marker remain unchanged and
must not be auto-settled or used as a new authorization. No further
paid Kie.ai API calls. Use only BCC_KIE_TERRA_STATUS.cmd to inspect
local attempt state, without touching the legacy Claude record.

Scope: optional developer gateway branch ONLY. Neither Windows
Modern Qt E2E nor live BCC trading operation was validated.

## Kie.ai dashboard result preview — 18 ноября 2026, после первой проверки

На втором пользовательском скриншоте страницы Kie.ai Logs показана
всплывающая карточка **«Предпросмотр результата»** для успешной
строки `gpt-6-1-terra`: значение **`BCC_TERRA_CONNECTED`**.
Оно в точности соответствует заранее заданному тестовому
`EXPECTED_TEXT`. Таким образом подтверждено, что *провайдер
сгенерировал корректный текст и показал его в своём журнале*.
Списание на том же экране — **0,01 кредита Kie.ai**, не
верифицированная сумма в долларах.

Это всё ещё **не** свидетельство того, что локальный Python-клиент
получил полноценный HTTP 200 body с ожидаемыми ключами
`status`, `output`, `usage`, идентификатором модели и
одним завершённым assistant/message. Поэтому **не установлена
точная первопричина** локального `BLOCKED/UNKNOWN`: это
могло быть исключение при чтении ответа или строгая проверка
формата данных, а также менее вероятные клиентские причины.
Предпросмотр Kie.ai — не полный JSON.

Для устранения системной связности изменены только офлайн-модули
ветки агента:
- `parse_content_response` теперь проверяет текст, идентификатор
  модели, статус завершения и запрет вызовов инструментов
  **отдельно** от счётчиков токенов;
- `parse_response` по-прежнему строго проверяет usage и отвергает
  неизвестные категории, поскольку эти поля нельзя считать
  подтверждённым счётом;
- чистая функция `assess_terra_response` возвращает
  `CONTENT_CONFIRMED_USAGE_UNVERIFIED`, если безопасный текст
  и ожидаемая модель совпадают, но детали учёта не проходят
  строгую проверку. Это **не** статус полноценной приёмки,
  не подтверждение USD-биллинга и не разрешение повторного POST.
- добавлены семь офлайн-регрессий: отсутствие usage, дополнительные
  категории, проверка запрета инструментов и неправильный текст.

Общий пакет стал **17 модулей / 238 тестовых методов**.
Новые тесты **ещё не выполнялись на Windows**; старый PASS
218/218 относится исключительно к ранее проверенному SHA.
**Реальный платный запрос не повторять**. Старый
`KNOWN_TERRA_AUTHORIZATION_CONSUMED=True` остаётся
неизменным; существующий HOLD и одноразовый маркер сохраняются.

Если понадобится диагностировать точную ошибку, достаточно
ранее полученного *полного* обезличенного JSON-ответа провайдера
или безопасной записи его схемы с типами полей. Новый запрос
с этой целью не нужен.
