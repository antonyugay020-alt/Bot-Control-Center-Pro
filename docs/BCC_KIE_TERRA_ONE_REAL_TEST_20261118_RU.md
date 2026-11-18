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
(16 тестовых модулей, минимум 216 проверок) и ввода в интерактивный
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
Новый source SHA и пакет из 216 тестов ещё должны быть проверены Windows.
