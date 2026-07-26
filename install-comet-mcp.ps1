# ============================================
#  Установка Comet MCP для LM Studio
#  (Perplexity Comet через MCP)
#  Windows 11 — финальная версия
#  Фикс: полный путь к npx.cmd + Path для LM Studio
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

function Get-NpxCmdPath {
    $candidates = @(
        "C:\Program Files\nodejs\npx.cmd",
        "${env:ProgramFiles}\nodejs\npx.cmd",
        "${env:ProgramFiles(x86)}\nodejs\npx.cmd"
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    $fromWhere = Get-Command npx.cmd -ErrorAction SilentlyContinue
    if ($fromWhere -and $fromWhere.Source) { return $fromWhere.Source }
    return $null
}

function Get-NodeDir {
    $nodeCmd = Get-Command node -ErrorAction SilentlyContinue
    if ($nodeCmd -and $nodeCmd.Source) {
        return (Split-Path -Parent $nodeCmd.Source)
    }
    if (Test-Path "C:\Program Files\nodejs\node.exe") {
        return "C:\Program Files\nodejs"
    }
    return "C:\Program Files\nodejs"
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

# 4. Ищем npx.cmd (LM Studio не видит PATH)
Write-Host "`n[4/5] Ищу npx.cmd..." -ForegroundColor Yellow

$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

$npxCmd = Get-NpxCmdPath
$nodeDir = Get-NodeDir

if (-not $npxCmd) {
    Write-Host "npx.cmd не найден! Проверь установку Node.js." -ForegroundColor Red
    exit 1
}

# JSON-экранирование пути (обратные слэши → \\ )
$npxCmdJson = $npxCmd -replace "\\", "\\"
$nodeDirJson = $nodeDir -replace "\\", "\\"

Write-Host "npx.cmd: $npxCmd" -ForegroundColor Green
Write-Host "Node dir: $nodeDir" -ForegroundColor Green

# 5. Создаём рабочий mcp.json-фрагмент для LM Studio
Write-Host "`n[5/5] Создаю готовый фрагмент mcp.json..." -ForegroundColor Yellow

$configDir = "$env:USERPROFILE\comet-mcp-config"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

$mcpSnippet = @"
{
  "mcpServers": {
    "comet-bridge": {
      "command": "$npxCmdJson",
      "args": ["-y", "perplexity-comet-mcp"],
      "env": {
        "Path": "$nodeDirJson;C:\\Windows\\System32"
      }
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
Write-Host "   • Вставь это (уже с полным путём — LM Studio иначе не видит node/npx):" -ForegroundColor White
Write-Host ""
Write-Host $mcpSnippet -ForegroundColor Gray
Write-Host ""
Write-Host "3. Сохрани файл → Restart у mcp/comet-bridge" -ForegroundColor White
Write-Host "4. Красный треугольник должен пропасть" -ForegroundColor White
Write-Host ""
Write-Host "Готовый файл также лежит здесь:" -ForegroundColor Yellow
Write-Host $snippetPath -ForegroundColor Cyan
Write-Host ""
