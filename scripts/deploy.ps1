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
    [string]$JobsFunctionName = "la-weather-jobs"
)

$ErrorActionPreference = "Stop"

# --- Читаем .env (KEY=VALUE, строки с # — комментарии) ----------------------
if (-not (Test-Path $EnvFile)) {
    throw "Файл $EnvFile не найден — скопируйте .env.example в .env и заполните."
}
$dotenv = @{}
foreach ($line in Get-Content $EnvFile) {
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

# --- Собираем zip с исходниками (без __pycache__ и прочего мусора) ----------
$stage = Join-Path $env:TEMP "la-weather-deploy-stage"
$zip = Join-Path $env:TEMP "la-weather-deploy.zip"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage | Out-Null
Copy-Item app -Destination (Join-Path $stage "app") -Recurse
Copy-Item handler.py, requirements.txt -Destination $stage
Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip
Write-Host "Архив собран: $zip"

# --- Функции: создать при первом запуске, залить новую версию ---------------
function Ensure-Function([string]$name) {
    yc serverless function get --name $name *> $null
    if ($LASTEXITCODE -ne 0) {
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
Ensure-Function $JobsFunctionName
# Таймауты: webhook до 120с (ленивый /forecast качает бюллетень NBS ~28 МБ),
# джобы до 300с (fetch тянет несколько бюллетеней с ретраями).
Deploy-Version $BotFunctionName "handler.bot_webhook" "120s"
Deploy-Version $JobsFunctionName "handler.job" "300s"

# --- Подсказка следующего шага ----------------------------------------------
$botId = (yc serverless function get --name $BotFunctionName --format json |
    ConvertFrom-Json).id
Write-Host ""
Write-Host "Готово. URL webhook-функции: https://functions.yandexcloud.net/$botId"
Write-Host "Если webhook ещё не установлен (или функция пересоздавалась):"
Write-Host "  yc serverless function allow-unauthenticated-invoke $BotFunctionName"
Write-Host "  python -m scripts.set_webhook https://functions.yandexcloud.net/$botId"
