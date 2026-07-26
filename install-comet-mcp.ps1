# ============================================
#  Установка Comet MCP для LM Studio
#  (Perplexity Comet через MCP)
#  Windows 11 — финальная версия
# ============================================

Write-Host "`n=== Установка Comet MCP для LM Studio ===" -ForegroundColor Cyan
Write-Host "Внимание: это облачное решение (Perplexity), не локальное!" -ForegroundColor Yellow

function Test-Node {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($node) {
        try {
            $ver = & node -v 2>$null
            if ($ver) { return $true }
        } catch {}
    }
    return $false
}

# 1. Проверка / установка Node.js
Write-Host "`n[1/5] Проверяю Node.js..." -ForegroundColor Yellow

if (Test-Node) {
    $nodeVersion = node -v
    Write-Host "Node.js уже установлен: $nodeVersion" -ForegroundColor Green
} else {
    Write-Host "Node.js не найден. Пытаюсь установить..." -ForegroundColor Yellow

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "Использую winget для установки Node.js LTS..." -ForegroundColor Cyan
        winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
        
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        Start-Sleep -Seconds 3
    }

    if (-not (Test-Node)) {
        Write-Host "winget не сработал. Скачиваю официальный установщик..." -ForegroundColor Yellow
        
        $tempDir = "$env:TEMP\node-install"
        New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
        $msiPath = "$tempDir\node-lts.msi"
        $url = "https://nodejs.org/dist/v22.17.0/node-v22.17.0-x64.msi"
        
        try {
            Invoke-WebRequest -Uri $url -OutFile $msiPath -UseBasicParsing
            Start-Process msiexec.exe -ArgumentList "/i `"$msiPath`" /qn /norestart" -Wait -Verb RunAs
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
            Start-Sleep -Seconds 3
        }
        catch {
            Write-Host "Не удалось установить Node.js автоматически." -ForegroundColor Red
            Write-Host "Скачай вручную: https://nodejs.org/" -ForegroundColor Yellow
            exit 1
        }
    }

    if (Test-Node) {
        Write-Host "Node.js успешно установлен: $(node -v)" -ForegroundColor Green
    } else {
        Write-Host "Node.js не виден. Закрой PowerShell и запусти скрипт заново." -ForegroundColor Red
        exit 1
    }
}

# 2. Проверка npm
Write-Host "`n[2/5] Проверяю npm..." -ForegroundColor Yellow
$npmVersion = npm -v
Write-Host "npm найден: $npmVersion" -ForegroundColor Green

# 3. Установка пакета (с подавлением предупреждений)
Write-Host "`n[3/5] Устанавливаю perplexity-comet-mcp..." -ForegroundColor Yellow

$oldErrorAction = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"

npm install -g perplexity-comet-mcp 2>$null

$installSuccess = $LASTEXITCODE -eq 0

if (-not $installSuccess) {
    Write-Host "Первая попытка не удалась, пробую с --force..." -ForegroundColor Yellow
    npm install -g perplexity-comet-mcp --force 2>$null
    $installSuccess = $LASTEXITCODE -eq 0
}

$ErrorActionPreference = $oldErrorAction

if ($installSuccess) {
    Write-Host "Пакет успешно установлен." -ForegroundColor Green
} else {
    Write-Host "Не удалось установить пакет." -ForegroundColor Red
    Write-Host "Запусти PowerShell от имени администратора и попробуй снова." -ForegroundColor Yellow
    exit 1
}

# 4. Проверка команды
Write-Host "`n[4/5] Проверяю команду..." -ForegroundColor Yellow

$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

$cometCmd = Get-Command perplexity-comet-mcp -ErrorAction SilentlyContinue
if ($cometCmd) {
    Write-Host "Команда perplexity-comet-mcp доступна." -ForegroundColor Green
} else {
    Write-Host "Команда пока не видна в PATH (это нормально)." -ForegroundColor Yellow
    Write-Host "Она появится после перезапуска терминала или LM Studio." -ForegroundColor Yellow
}

# 5. Создаём конфиг
Write-Host "`n[5/5] Создаю готовый фрагмент mcp.json..." -ForegroundColor Yellow

$configDir = "$env:USERPROFILE\comet-mcp-config"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

$mcpSnippet = @"
{
  "mcpServers": {
    "comet-bridge": {
      "command": "perplexity-comet-mcp"
    }
  }
}
"@

$snippetPath = "$configDir\mcp-snippet.json"
$mcpSnippet | Out-File -FilePath $snippetPath -Encoding utf8

Write-Host "Фрагмент сохранён: $snippetPath" -ForegroundColor Cyan

Write-Host "`n========================================" -ForegroundColor Green
Write-Host "  Установка завершена!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green

Write-Host "`nЧто делать дальше:" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Скачай и установи браузер Comet:" -ForegroundColor White
Write-Host "   https://www.perplexity.ai/comet" -ForegroundColor Cyan
Write-Host ""
Write-Host "2. В LM Studio:" -ForegroundColor White
Write-Host "   • Справа → Program" -ForegroundColor White
Write-Host "   • Install → Edit mcp.json" -ForegroundColor White
Write-Host "   • Вставь это:" -ForegroundColor White
Write-Host ""
Write-Host $mcpSnippet -ForegroundColor Gray
Write-Host ""
Write-Host "3. Сохрани файл и включи сервер comet-bridge" -ForegroundColor White
Write-Host ""
Write-Host "Готовый файл также лежит здесь:" -ForegroundColor Yellow
Write-Host $snippetPath -ForegroundColor Cyan
Write-Host ""
