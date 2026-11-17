# BCC: единственный разрешённый тест Claude через Kie.ai (до $0,01)

## Разрешение и область действия

Пользователь 17.11.2026 разрешил **ОДИН** платный тест Claude Sonnet 5.5
через Kie.ai с денежным пределом $0,01. Разрешение не распространяется на
последующие запросы, разработку BCC силами Claude или автоматический агент.

Изолированная ветка: agent/bcc-work-claude-gateway-20261117.
Ключ уже сохранён в Windows Credential Manager:
BCC.DevAgent.KieAI.APIKey.v1. Повторно вводить его не нужно.

GPT Work не имеет доступа к Windows Credential Manager и поэтому не может
сам отправить запрос с этого компьютера. Один тест отправляет только
локальная программа по явному действию пользователя.

## Порядок исполнения на Windows PowerShell

1. Обновить ранее созданный чистый checkout:
   
       $dir = "$HOME\Desktop\BCC_AGENT_GATEWAY"
       $branch = "agent/bcc-work-claude-gateway-20261117"
       if (-not (Test-Path "$dir\.git")) { throw "Нет отдельной папки агента" }
       if ((git -C $dir branch --show-current).Trim() -ne $branch) { throw "Неверная ветка" }
       if (git -C $dir status --porcelain) { throw "Есть локальные изменения" }
       git -C $dir pull --ff-only
       if ($LASTEXITCODE -ne 0) { throw "Git pull failed" }
       & "$dir\BCC_KIE_ONE_TEST_WINDOWS.cmd"

2. Запускатель **сначала выполняет offline-регрессию** всех
   test_bcc_kie_*.py. При сбое хоть одного теста запрос не отправляется.

3. Программа запрашивает отдельное локальное подтверждение.
   Для выполнения разрешённой пользователем ОДНОЙ попытки ввести
   **APPROVE 0.01**, затем Enter. Любой другой ответ отменяет тест.

4. Клиент считывает ключ локально из Credential Manager, создаёт
   резерв $0,01 в SQLite %LOCALAPPDATA%\BCCDevAgent\claude_budget.sqlite3
   до сети, и выполняет один HTTPS POST:
   https://api.kie.ai/claude/v1/messages, модель claude-sonnet-5-5.
   Сообщение строго фиксировано: "Reply with exactly BCC_KIE_CONNECTED
   and nothing else." Выход ограничен 32 токенами. Никаких инструментов,
   streaming, истории BCC, работы с GitHub, логов торговых ботов.

5. Возвращается только обезличенный статус, HTTP-код, счётчики токенов,
   оценочная стоимость. Значение API-ключа и сырой ответ не выводятся.
   При ошибке, таймауте, 401/503 или разрыве **повтора нет**. Идентификатор
   запроса уже зарезервирован и блокирует новые попытки этого теста.

## Особенности денежного ограничения

Резерв $0,01 вычисляется консервативно по опубликованным ставкам Kie.ai
Sonnet 5.5 ($0,80/1M input, $4/1M output), с моделью расхода
12 000 входных + 100 выходных токенов. Фактическое сообщение много меньше.

**Локальный резерв НЕ может обеспечить провайдерский абсолютный лимит.**
Провайдер может иметь отличающиеся тарифы, минимальные платежи, комиссии
или тарификацию кэша. Перед подтверждением пользователь видит
предупреждение, что точную сумму нужно сверить в Kie.ai.
Если провайдерское ограничение в $0,01 обязательно в буквальном
смысле, запрос следует отменить до настройки подтверждённого
серверного лимита. Программно остановить уже выставленный счёт невозможно.

Не вызывается settle() с оценкой токенов: резерв остаётся HELD,
пока не будет сверки с фактическим провайдерским биллингом.
Общий месячный бюджет Kie.ai остаётся $20; для этого теста отдельно
зарезервировано $0,01. Ни один последующий вызов не разрешён.

## Не затрагивает

- Работающие BCC-боты, процессы и мониторы на Windows;
- ключи торговых бирж, SQLite-базы живых стратегий;
- PR #11, production/main, AI SMM, Bybit AI Trader;
- GitHub Actions, платные сервисы кроме этой одной попытки Kie.ai.

## Доказательства и нерешённое

Изолированные тесты tests/test_bcc_kie_one_shot.py проверяют
подтверждение, блокировку второго запуска, резерв до сети,
неизвестный результат, 401/503 и отсутствие автоматических повторов
на подставных ответах.

Пока пользователь не выполнил этот запуск на Windows, фактический
ответ Kie.ai и списание не подтверждены. Это не запуск автономного
Claude-программиста и не полноценная интеграция GPT Work + Graphify.


## FIRST_LIVE_ATTEMPT_STATUS — 2026-11-17, ~23:37 local UTC+5

User provided PowerShell screenshot after exercising the one-time authorized
live test on their Windows PC.

Observed:
- Offline regression: 74 tests, 2 skipped, zero failed.
- An earlier invocation was CANCELLED before any provider attempt.
- A subsequent invocation used the one-time confirmation and produced:
  result=HTTP_ERROR_NO_RETRY; http_status=502;
  provider_charge=UNKNOWN; reservation_usd=0.010000;
  attempts_allowed_remaining=0.
- No Claude completion, no validated input/output usage, no proven spend.
- Local ledger holds the USD 0.01 reservation pending provider reconciliation.
  The hold is not evidence of a $0.01 charge by Kie.ai.

SECURITY STATE:
- The specific user's single paid-test authorization is CONSUMED.
- DO NOT rerun BCC_KIE_ONE_TEST_WINDOWS.cmd as a paid attempt, DO NOT
  erase/replace the SQLite ledger to enable a second try, and DO NOT
  reuse or repurpose this authorization.
- No additional paid call is approved. Real Claude programming and
  GPT Work/Graphify integration are still NOT CONNECTED.
- Preserve the original ledger at
  %LOCALAPPDATA%\BCCDevAgent\claude_budget.sqlite3.

Read-only next evidence to request from user: Kie.ai dashboard API Usage /
Logs around 2026-11-17 18:37 UTC (23:37 UTC+5) for this single
POST /claude/v1/messages, model claude-sonnet-5-5, including any request ID,
status, billable tokens and charge. Never request the API key or other
secrets, and do not expose raw Authorization headers.

The client suppresses the response body for non-200 HTTP statuses,
so the screenshot and repository alone cannot distinguish provider
outage, upstream gateway failure or rejected payload/account.
A new paid test requires NEW explicit user approval and reconciled billing.


## KIE_PROVIDER_LOG_RECONCILIATION — 2026-11-17

User supplied a screenshot of the authenticated Kie.ai Logs page for the
same attempt, timestamp 2026-11-17 23:37:42 (user's local UTC+5).

Provider-visible evidence:
- Model: claude-sonnet-5-5.
- Request status: error.
- Result type: error, nested error type: api_error.
- Result message: "Internal error, please try again later".
- Credits used displayed by Kie.ai Logs: **0**.
- Windows client received HTTP 502 and did not receive a Claude completion.

Interpretation: The request is recorded on Kie.ai and its own log shows zero
credits consumed for this row. This supports no charge for that request in the
log, but is not an independently audited billing statement. The exact internal
cause of the gateway failure is unknown and cannot be established from
these two screenshots. The saved key is present locally; success of the API
credential and the model response is still NOT verified.

The local $0.01 reservation is a HOLD, not a provider debit. Do not
silently delete, rewrite, or bypass the one-time ledger record. Reconcile it
only using a reviewed, read-only provider billing statement and an
explicitly controlled ledger settlement procedure. Do not retry the spent
authorization or make additional paid calls. The BCC agent stays disabled
for live Claude calls, and other repositories/trading systems remain
untouched.
