# deploy.ps1 — деплой обеих Cloud Functions из одного архива кода (yc CLI).
# Повторяемая часть деплоя: собирает zip (app/ + handler.py + requirements.txt),
# создаёт функции при первом запуске и заливает новые версии с переменными
# окружения из .env. Разовая настройка облака (сервисный аккаунт, база YDB,
# таймер-триггеры, публичный доступ к webhook) — командами из README.
#
# Требуется: установленный и настроенный yc CLI (yc init), заполненный .env
# (BOT_TOKEN, ALLOWED_CHAT_IDS, YDB_DATABASE, TG_WEBHOOK_SECRET,
# YC_SERVICE_ACCOUNT_ID — см. .env.example).
#
# Запуск из корня проекта:  powershell -File scripts\deploy.ps1

param(
    [string]$EnvFile = ".env",
    [string]$BotFunctionName = "la-weather-bot-webhook",
    [string]$PollFunctionName = "la-weather-bot-poll",
    [string]$JobsFunctionName = "la-weather-jobs"
)

$ErrorActionPreference = "Stop"

# Путь проекта содержит квадратные скобки ([ FILES ]) — в PowerShell 5.1 из-за
# этого ломаются ВСЕ относительные пути (даже с -LiteralPath), пока текущая
# директория «скобочная». Поэтому: никакой опоры на текущую директорию, все
# пути проекта — абсолютные, от расположения самого скрипта, и только через
# -LiteralPath.
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not [System.IO.Path]::IsPathRooted($EnvFile)) {
    $EnvFile = Join-Path $projectRoot $EnvFile
}

# --- Читаем .env (KEY=VALUE, строки с # — комментарии) ----------------------
if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Файл $EnvFile не найден — скопируйте .env.example в .env и заполните."
}
$dotenv = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    $line = $line.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $idx = $line.IndexOf("=")
        $dotenv[$line.Substring(0, $idx).Trim()] = $line.Substring($idx + 1).Trim()
    }
}
foreach ($key in @("BOT_TOKEN", "ALLOWED_CHAT_IDS", "YDB_ENDPOINT", "YDB_DATABASE",
                   "TG_WEBHOOK_SECRET", "YC_SERVICE_ACCOUNT_ID")) {
    if (-not $dotenv[$key]) { throw "В $EnvFile не заполнено $key" }
}

# --- Собираем zip с исходниками ---------------------------------------------
# Через python (scripts/build_zip.py), а не Compress-Archive: архивы PowerShell
# YCF распаковывает с нечитаемыми правами, и рантайм падает с
# «No module named 'app'». build_zip.py выставляет права явно.
$zip = Join-Path $env:TEMP "la-weather-deploy.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
python (Join-Path $PSScriptRoot "build_zip.py") --root $projectRoot --out $zip
if ($LASTEXITCODE -ne 0) { throw "Сборка архива не удалась" }
Write-Host "Архив собран: $zip"

# --- Функции: создать при первом запуске, залить новую версию ---------------
function Ensure-Function([string]$name) {
    # «Не найдена» — ожидаемый исход, а не сбой. В PS 5.1 перенаправление stderr
    # нативной команды при ErrorActionPreference=Stop делает такую ошибку
    # фатальной, поэтому на время проверки ослабляем реакцию и смотрим exit code.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        yc serverless function get --name $name *> $null
        $missing = ($LASTEXITCODE -ne 0)
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if ($missing) {
        Write-Host "Функция $name не найдена — создаю..."
        yc serverless function create --name $name | Out-Null
    }
}

# Окружение функций. ALLOWED_CHAT_IDS может содержать запятые, поэтому каждый
# ключ — отдельным флагом --environment (одним флагом со списком нельзя).
$envArgs = @(
    "--environment", "DB_BACKEND=ydb",
    "--environment", "YDB_METADATA_CREDENTIALS=1",   # IAM сервисного аккаунта функции
    "--environment", "YDB_ENDPOINT=$($dotenv['YDB_ENDPOINT'])",
    "--environment", "YDB_DATABASE=$($dotenv['YDB_DATABASE'])",
    "--environment", "BOT_TOKEN=$($dotenv['BOT_TOKEN'])",
    "--environment", "ALLOWED_CHAT_IDS=$($dotenv['ALLOWED_CHAT_IDS'])",
    "--environment", "TG_WEBHOOK_SECRET=$($dotenv['TG_WEBHOOK_SECRET'])"
)
if ($dotenv["HTTP_CONTACT"]) {
    $envArgs += @("--environment", "HTTP_CONTACT=$($dotenv['HTTP_CONTACT'])")
}

function Deploy-Version([string]$name, [string]$entrypoint, [string]$timeout) {
    Write-Host "Заливаю версию $name ($entrypoint)..."
    yc serverless function version create `
        --function-name $name `
        --runtime python312 `
        --entrypoint $entrypoint `
        --memory 512m `
        --execution-timeout $timeout `
        --service-account-id $dotenv["YC_SERVICE_ACCOUNT_ID"] `
        --source-path $zip `
        @envArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Деплой $name не удался" }
}

Ensure-Function $BotFunctionName
Ensure-Function $PollFunctionName
Ensure-Function $JobsFunctionName
# Таймауты: webhook до 120с (ленивый /forecast качает бюллетень NBS ~28 МБ),
# джобы до 300с (fetch тянет несколько бюллетеней с ретраями).
Deploy-Version $BotFunctionName "handler.bot_webhook" "120s"
Deploy-Version $PollFunctionName "handler.bot_poll" "120s"
Deploy-Version $JobsFunctionName "handler.job" "300s"

# Polling используется вместо webhook: Telegram не всегда может установить
# входящее соединение с публичными endpoint'ами Yandex Cloud, тогда как
# исходящие getUpdates/sendMessage работают стабильно.
$pollTriggerName = $PollFunctionName
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    yc serverless trigger get --name $pollTriggerName *> $null
    $missingPollTrigger = ($LASTEXITCODE -ne 0)
} finally {
    $ErrorActionPreference = $prevEap
}
if ($missingPollTrigger) {
    Write-Host "Создаю минутный Telegram polling-триггер..."
    yc serverless trigger create timer `
        --name $pollTriggerName `
        --cron-expression '* * ? * * *' `
        --payload '{}' `
        --invoke-function-name $PollFunctionName `
        --invoke-function-service-account-id $dotenv["YC_SERVICE_ACCOUNT_ID"] | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Создание polling-триггера не удалось" }
}

# --- Итог -------------------------------------------------------------------
$pollId = (yc serverless function get --name $PollFunctionName --format json |
    ConvertFrom-Json).id
Write-Host ""
Write-Host "Готово. Telegram polling-функция: $pollId"
Write-Host "Триггер: $pollTriggerName (раз в минуту)"
Write-Host "Webhook должен оставаться снятым: python -m scripts.set_webhook --delete"
