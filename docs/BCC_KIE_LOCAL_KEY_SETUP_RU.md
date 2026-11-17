# BCC — ввод ключа Kie.ai через Ctrl+V

**OFFLINE ONLY / NOT RELEASE.** Эта утилита сохраняет секрет для отдельного агента разработки BCC. Она не вызывает Claude, не отправляет HTTP-запросы, не управляет ботами и не запускает GitHub Actions.

## Запуск, если папка BCC_AGENT_GATEWAY уже создана

В PowerShell:

    $agent = "$HOME\Desktop\BCC_AGENT_GATEWAY"
    if (-not (Test-Path "$agent\.git")) { throw "Нет клона BCC_AGENT_GATEWAY. Сначала клонируйте ветку." }
    if ((git -C $agent branch --show-current).Trim() -ne "agent/bcc-work-claude-gateway-20261117") { throw "Неверная ветка. Не обновляю рабочую папку." }
    if (git -C $agent status --porcelain) { throw "Есть локальные изменения. Не обновляю." }
    git -C $agent pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "Git pull не выполнен" }
    & "$agent\BCC_KIE_KEY_WINDOWS.cmd"

Если папки нет, клонируйте отдельно от работающих ботов:

    $agent = "$HOME\Desktop\BCC_AGENT_GATEWAY"
    if (Test-Path -LiteralPath $agent) { throw "Каталог существует. Не перезаписывайте его." }
    git clone --depth 1 --single-branch --branch agent/bcc-work-claude-gateway-20261117 https://github.com/antonyugay020-alt/Bot-Control-Center-Pro.git "$agent"
    if ($LASTEXITCODE -ne 0) { throw "Git clone не выполнен" }
    & "$agent\BCC_KIE_KEY_WINDOWS.cmd"

## Как ввести ключ

Откроется отдельное окно **BCC — Kie.ai API key**, а не старый консольный ввод.

1. В первое скрытое поле вставить ключ **Ctrl+V** (либо нажать **Paste** справа).
2. Повторить вставку во второе поле.
3. Нажать **Save key**.

Оба поля показывают звёздочки, не символы ключа. При повторном сохранении программа попросит подтвердить замену. В конце она сообщит **Key stored locally. No API request made.** Не присылайте ключ в чат. Вставленный секрет остаётся в буфере обмена Windows, пока вы сами не замените содержимое буфера.

## Где хранится и как проверять

**Windows Credential Manager / Generic Credential**:
**BCC.DevAgent.KieAI.APIKey.v1**. Для контроля без отображения содержимого:

    $agent = "$HOME\Desktop\BCC_AGENT_GATEWAY"
    py -3 -I "$agent\tools\bcc_kie_credentials.py" status

Для удаления: заменить status на remove, затем ввести DELETE в консоли.

Окно требует обычный Windows Python 3 с **Tkinter**. Если установлен Python без Tkinter, выводится сообщение об отсутствии GUI и ничего не сохраняется. Не нужно запускать от администратора, останавливать ботов, создавать VM или перезагружать Windows.

## Важные ограничения

- Подготовлен только безопасный ввод/хранение ключа на локальном Windows-ПК. Облачный GPT Work его автоматически не получает; Kie.ai и Claude пока **не подключены**.
- Запрет платных API-вызовов сохраняется. $20/мес — бюджетный потолок, но **не разрешение** на отдельный вызов. Локальная система контроля расходов — независимый прототип.
- Приложения с правами того же Windows-пользователя потенциально могут прочитать Credential Manager. Это не защита от заражённого компьютера; Python не гарантирует очистку всех копий секретов в памяти.
- Никаких изменений работающих BCC-ботов, баз данных, торговых настроек, ключей биржи или основной ветки GitHub не производится.
- Шесть offline unit-тестов для GUI Ctrl+V, замены выделения, отказа при ошибочном содержимом и защиты от повторной записи прошли в Linux с изолированной имитацией Windows-хранилища. **Нативный GUI и реальная запись в Windows ещё не проверены; полная BCC и Windows Qt-приёмка не выполнялись.**
