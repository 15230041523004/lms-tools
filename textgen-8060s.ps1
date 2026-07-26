#requires -Version 5.1
<#
  textgen-8060s.ps1  —  install / run / uninstall
  AMD Radeon 8060S | Portable TextGen (oobabooga) | default ROCm

  Run:
    powershell -ExecutionPolicy Bypass -File textgen-8060s.ps1
    powershell -ExecutionPolicy Bypass -File textgen-8060s.ps1 -Backend Vulkan
    powershell -ExecutionPolicy Bypass -File textgen-8060s.ps1 -Reinstall
    powershell -ExecutionPolicy Bypass -File textgen-8060s.ps1 -NoRun

  Uninstall:
    powershell -ExecutionPolicy Bypass -File textgen-8060s.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [ValidateSet("ROCm", "Vulkan", "Ask")]
    [string]$Backend = "Ask",

    [string]$InstallRoot = "C:\textgen-8060s",

    [int]$ListenPort = 7860,
    [int]$ApiPort = 5000,

    [switch]$Reinstall,
    [switch]$NoRun,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ----- reliable console input (blinking cursor) -----
function Read-ConsoleLine {
    param(
        [string]$Prompt = "",
        [string]$Default = ""
    )
    try {
        if (-not [Console]::KeyAvailable) { }
    } catch {}

    if ($Prompt) {
        if ($Default) {
            Write-Host -NoNewline ($Prompt + " [" + $Default + "]: ")
        } else {
            Write-Host -NoNewline ($Prompt + ": ")
        }
    }

    try { [Console]::Out.Flush() } catch {}

    $line = $null
    try {
        $line = [Console]::ReadLine()
    } catch {
        $line = Read-Host
    }

    if ($null -eq $line) { $line = "" }
    $line = $line.Trim()
    if (($line -eq "") -and ($Default -ne "")) {
        return $Default
    }
    return $line
}

function Write-Step([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }

function Set-Utf8NoBom([string]$Path, [string]$Value) {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Value, $enc)
}

function Get-AllLanIPv4 {
    $list = @()
    try {
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object {
                $_.IPAddress -notlike "127.*" -and
                $_.IPAddress -notlike "169.254.*" -and
                $_.AddressState -eq "Preferred"
            } |
            ForEach-Object {
                $n = $null
                try { $n = (Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -EA SilentlyContinue).Name } catch {}
                if (-not $n) { $n = "if$($_.InterfaceIndex)" }
                $list += [pscustomobject]@{ IP = $_.IPAddress; Name = $n }
            }
    } catch {
        try {
            Get-CimInstance Win32_NetworkAdapterConfiguration -Filter "IPEnabled=True" -EA Stop |
                ForEach-Object {
                    $desc = $_.Description
                    foreach ($ip in @($_.IPAddress)) {
                        if ($ip -match '^\d+\.\d+\.\d+\.\d+$' -and $ip -notlike "127.*" -and $ip -notlike "169.254.*") {
                            $list += [pscustomobject]@{ IP = $ip; Name = $desc }
                        }
                    }
                }
        } catch {}
    }
    $list | Sort-Object IP -Unique
}

function Show-Urls {
    Write-Host ""
    Write-Host "Local:  http://127.0.0.1:$ListenPort" -ForegroundColor Green
    Write-Host "API:    http://127.0.0.1:$ApiPort/v1" -ForegroundColor Green
    Write-Host "Listen: 0.0.0.0 (all interfaces)" -ForegroundColor Cyan
    $lan = Get-AllLanIPv4
    if ($lan) {
        Write-Host "LAN:" -ForegroundColor Cyan
        foreach ($x in $lan) {
            Write-Host ("  http://{0}:{1}  [{2}]" -f $x.IP, $ListenPort, $x.Name)
        }
    }
    Write-Host ""
}

function Stop-TextGenProcesses {
    try {
        Get-Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Path -and ($_.Path -like ($InstallRoot + "\*"))
            } |
            ForEach-Object {
                try {
                    Write-Host ("Stop PID {0} ({1})" -f $_.Id, $_.ProcessName)
                    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
                } catch {}
            }
    } catch {}
    Start-Sleep -Seconds 1
}

function Remove-TextGenFirewall {
    foreach ($p in @($ListenPort, $ApiPort)) {
        $rn = "TextGen 8060S TCP $p"
        try {
            $rules = Get-NetFirewallRule -DisplayName $rn -ErrorAction SilentlyContinue
            if ($rules) {
                $rules | Remove-NetFirewallRule -ErrorAction SilentlyContinue
                Write-Host "Firewall removed: $rn"
            } else {
                Write-Host "Firewall not found: $rn"
            }
        } catch {
            Write-Host "Firewall skip: $rn"
        }
    }
}

function Remove-DirForce([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    try {
        & cmd.exe /d /c "rd /s /q `"\\?\$Path`""
    } catch {}
    Start-Sleep -Milliseconds 300
    if (Test-Path -LiteralPath $Path) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        } catch {
            return $false
        }
    }
    return -not (Test-Path -LiteralPath $Path)
}

# ===================== UNINSTALL =====================
if ($Uninstall) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host " TextGen 8060S UNINSTALL" -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host ("Target: " + $InstallRoot)
    Write-Host ""

    if (-not (Test-Path -LiteralPath $InstallRoot)) {
        Write-Host "Folder already missing." -ForegroundColor Green
        Remove-TextGenFirewall
        Write-Host ""
        $null = Read-ConsoleLine -Prompt "Press Enter to close"
        exit 0
    }

    $appPath    = Join-Path $InstallRoot "app"
    $dataPath   = Join-Path $InstallRoot "user_data"
    $logsPath   = Join-Path $InstallRoot "logs"
    $modelsPath = Join-Path $dataPath "models"

    Write-Host "Choose what to remove:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  1  EVERYTHING"
    Write-Host "     app + user_data + models + logs + firewall"
    Write-Host ""
    Write-Host "  2  APP ONLY  (keep models)"
    Write-Host "     delete app, keep user_data\models"
    Write-Host ""
    Write-Host "  3  APP + LOGS  (keep models)"
    Write-Host "     delete app + logs, keep user_data\models"
    Write-Host ""
    Write-Host "  4  CANCEL"
    Write-Host ""

    $choice = Read-ConsoleLine -Prompt "Enter 1 / 2 / 3 / 4" -Default "4"
    Write-Host ("Got: [" + $choice + "]")

    if ($choice -eq "4") {
        Write-Host "Cancelled. Nothing deleted."
        $null = Read-ConsoleLine -Prompt "Press Enter to close"
        exit 0
    }

    if ($choice -notin @("1", "2", "3")) {
        Write-Host "Invalid choice. Cancelled."
        $null = Read-ConsoleLine -Prompt "Press Enter to close"
        exit 1
    }

    Write-Host ""
    Write-Host ("You selected: " + $choice) -ForegroundColor Yellow
    $confirm = Read-ConsoleLine -Prompt "Type YES to confirm"
    Write-Host ("Got: [" + $confirm + "]")

    if ($confirm -cne "YES") {
        Write-Host "Cancelled (need exact YES)."
        $null = Read-ConsoleLine -Prompt "Press Enter to close"
        exit 1
    }

    Write-Step "Stop processes"
    Stop-TextGenProcesses

    $ok = $true

    if ($choice -eq "1") {
        Write-Step "Remove firewall"
        Remove-TextGenFirewall
        Write-Step "Delete entire folder"
        if (-not (Remove-DirForce $InstallRoot)) {
            $ok = $false
            Write-Host ("FAILED to delete: " + $InstallRoot) -ForegroundColor Red
        }
    }
    elseif ($choice -eq "2") {
        Write-Step "Delete app only"
        if (Test-Path $appPath) {
            if (-not (Remove-DirForce $appPath)) {
                $ok = $false
                Write-Host ("FAILED: " + $appPath) -ForegroundColor Red
            }
        }
        $bf = Join-Path $InstallRoot "BACKEND.txt"
        if (Test-Path $bf) { Remove-Item $bf -Force -EA SilentlyContinue }
        Write-Host ("Kept: " + $modelsPath + " (and user_data)")
    }
    elseif ($choice -eq "3") {
        Write-Step "Delete app + logs"
        if (Test-Path $appPath) {
            if (-not (Remove-DirForce $appPath)) {
                $ok = $false
                Write-Host ("FAILED: " + $appPath) -ForegroundColor Red
            }
        }
        if (Test-Path $logsPath) {
            if (-not (Remove-DirForce $logsPath)) {
                $ok = $false
                Write-Host ("FAILED: " + $logsPath) -ForegroundColor Red
            }
        }
        $bf = Join-Path $InstallRoot "BACKEND.txt"
        if (Test-Path $bf) { Remove-Item $bf -Force -EA SilentlyContinue }
        Write-Host ("Kept: " + $modelsPath + " (and user_data)")
    }

    Write-Host ""
    if ($ok) {
        Write-Host "Uninstall step finished OK." -ForegroundColor Green
    } else {
        Write-Host "Some paths not deleted. Close programs using the folder and retry." -ForegroundColor Red
    }

    if (Test-Path $InstallRoot) {
        Write-Host ("Still exists: " + $InstallRoot)
        try {
            Get-ChildItem $InstallRoot -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Host ("  - " + $_.Name) }
        } catch {}
    } else {
        Write-Host "Folder fully removed."
    }

    Write-Host ""
    $null = Read-ConsoleLine -Prompt "Press Enter to close"
    exit $(if ($ok) { 0 } else { 2 })
}

# ===================== INSTALL / RUN =====================
$AppDir      = Join-Path $InstallRoot "app"
$UserDataDir = Join-Path $InstallRoot "user_data"
$ModelsDir   = Join-Path $UserDataDir "models"
$LogsDir     = Join-Path $InstallRoot "logs"
$EntryBat    = Join-Path $AppDir "textgen.bat"
$FlagsFile   = Join-Path $UserDataDir "CMD_FLAGS.txt"
$BackendFile = Join-Path $InstallRoot "BACKEND.txt"

Write-Host ""
Write-Host "TextGen 8060S — install+run (single script)" -ForegroundColor Green
Write-Host ("Root: " + $InstallRoot)
Write-Host ""

$needInstall = $Reinstall -or -not (Test-Path $EntryBat)

if ($needInstall) {
    Write-Step "Install required"

    if ($Backend -eq "Ask") {
        Write-Host "Backend:"
        Write-Host "  1 / R / Enter = ROCm   (default)" -ForegroundColor Green
        Write-Host "  2 / V         = Vulkan"
        $ans = Read-ConsoleLine -Prompt "Choice" -Default "1"
        $ans = $ans.ToUpperInvariant()
        switch -Regex ($ans) {
            "^(1|R|ROCM)$"   { $Backend = "ROCm" }
            "^(2|V|VULKAN)$" { $Backend = "Vulkan" }
            default          { $Backend = "ROCm" }
        }
    }
    Write-Host ("Backend = " + $Backend) -ForegroundColor Green

    Write-Step "Fetch latest release"
    $headers = @{
        "User-Agent" = "textgen-8060s-one-script"
        "Accept"     = "application/vnd.github+json"
    }
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/oobabooga/textgen/releases/latest" -Headers $headers

    $selected = $Backend
    if ($selected -eq "ROCm") {
        $pat = "windows-rocm.*\.zip$"
    } else {
        $pat = "windows-vulkan\.zip$"
    }

    $asset = $release.assets | Where-Object { $_.name -match $pat } | Select-Object -First 1
    if (-not $asset -and $selected -eq "ROCm") {
        Write-Warning ("No ROCm zip in " + $release.tag_name + " -> Vulkan")
        $selected = "Vulkan"
        $pat = "windows-vulkan\.zip$"
        $asset = $release.assets | Where-Object { $_.name -match $pat } | Select-Object -First 1
    }
    if (-not $asset) { throw ("No Windows AMD portable zip in release " + $release.tag_name) }

    Write-Host ("Release: " + $release.tag_name)
    Write-Host ("Asset:   " + $asset.name)

    $TempRoot   = Join-Path $env:TEMP ("tg8060s-" + [guid]::NewGuid().ToString("N"))
    $ZipPath    = Join-Path $TempRoot $asset.name
    $ExtractDir = Join-Path $TempRoot "extract"
    New-Item -ItemType Directory -Force -Path $TempRoot, $ExtractDir | Out-Null

    try {
        Write-Step "Download"
        Invoke-WebRequest -Uri $asset.browser_download_url -Headers $headers -OutFile $ZipPath -UseBasicParsing

        if ($asset.digest -match "^sha256:(.+)$") {
            $exp = $Matches[1].ToLowerInvariant()
            $act = (Get-FileHash $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($act -ne $exp) { throw "SHA256 mismatch" }
            Write-Host "SHA256 OK" -ForegroundColor Green
        }

        Write-Step "Extract"
        $tar = Join-Path $env:SystemRoot "System32\tar.exe"
        if (-not (Test-Path $tar)) { throw "tar.exe not found" }
        & $tar -xf $ZipPath -C $ExtractDir
        if ($LASTEXITCODE -ne 0) { throw ("tar failed: " + $LASTEXITCODE) }

        $found = Get-ChildItem $ExtractDir -Recurse -File -Filter "textgen.bat" | Select-Object -First 1
        if (-not $found) { throw "textgen.bat not in archive" }
        $PackageRoot = $found.Directory.FullName

        if (Test-Path $AppDir) {
            Write-Step "Remove old app"
            Stop-TextGenProcesses
            [void](Remove-DirForce $AppDir)
        }
        New-Item -ItemType Directory -Force -Path $AppDir | Out-Null

        Write-Step "Copy app"
        & robocopy.exe $PackageRoot $AppDir /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /XJ /NFL /NDL /NP /NJH /NJS | Out-Null
        if ($LASTEXITCODE -ge 8) { throw ("robocopy failed: " + $LASTEXITCODE) }

        if (-not (Test-Path $EntryBat)) { throw ("After copy, missing: " + $EntryBat) }

        @(
            $UserDataDir, $ModelsDir, $LogsDir,
            (Join-Path $UserDataDir "characters"),
            (Join-Path $UserDataDir "presets"),
            (Join-Path $UserDataDir "instruction-templates"),
            (Join-Path $UserDataDir "grammars"),
            (Join-Path $UserDataDir "logs"),
            (Join-Path $UserDataDir "cache")
        ) | ForEach-Object { New-Item -ItemType Directory -Force -Path $_ | Out-Null }

        $flags = @"
--listen
--listen-host 0.0.0.0
--listen-port $ListenPort
--api
--api-port $ApiPort
--loader llama.cpp
--gpu-layers -1
--ctx-size 0
--cache-type q8_0
"@
        Set-Utf8NoBom $FlagsFile $flags
        Set-Utf8NoBom $BackendFile ($selected + "`nrelease=" + $release.tag_name + "`nasset=" + $asset.name + "`n")

        try {
            foreach ($p in @($ListenPort, $ApiPort)) {
                $rn = "TextGen 8060S TCP $p"
                if (-not (Get-NetFirewallRule -DisplayName $rn -EA SilentlyContinue)) {
                    New-NetFirewallRule -DisplayName $rn -Direction Inbound -Protocol TCP -LocalPort $p -Profile Any -Action Allow | Out-Null
                }
            }
        } catch {}

        Write-Host ("Install OK: " + $EntryBat) -ForegroundColor Green
        Write-Host ("Models dir: " + $ModelsDir)
    }
    finally {
        if (Test-Path $TempRoot) {
            [void](Remove-DirForce $TempRoot)
        }
    }
}
else {
    Write-Host ("Already installed: " + $EntryBat) -ForegroundColor Green
    if (Test-Path $BackendFile) {
        Write-Host ("Backend: " + (Get-Content $BackendFile -TotalCount 1))
    }
}

New-Item -ItemType Directory -Force -Path $UserDataDir, $ModelsDir, $LogsDir | Out-Null
if (-not (Test-Path $FlagsFile)) {
    $flags = @"
--listen
--listen-host 0.0.0.0
--listen-port $ListenPort
--api
--api-port $ApiPort
--loader llama.cpp
--gpu-layers -1
--ctx-size 0
--cache-type q8_0
"@
    Set-Utf8NoBom $FlagsFile $flags
}

Show-Urls

if ($NoRun) {
    Write-Host "NoRun set — exit without starting."
    exit 0
}

if (-not (Test-Path $EntryBat)) {
    Write-Host ("FATAL: still no textgen.bat at " + $EntryBat) -ForegroundColor Red
    $null = Read-ConsoleLine -Prompt "Press Enter to close"
    exit 2
}

Write-Step "Starting TextGen"
$env:NO_COLOR = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $LogsDir ("textgen-" + $stamp + ".log")
Write-Host ("Log: " + $logPath)

$exitCode = 0
Push-Location $AppDir
try {
    try { Start-Transcript -Path $logPath -Force | Out-Null } catch {}
    & $EntryBat --user-data-dir $UserDataDir
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) { $exitCode = 0 }
}
catch {
    $exitCode = 1
    Write-Host $_.Exception.ToString() -ForegroundColor Red
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
    Pop-Location
}

if (Test-Path $logPath) {
    $t = Get-Content $logPath -Raw -EA SilentlyContinue
    if ($t) {
        $m = [regex]::Matches($t, "Server process exited with code\s+(-?\d+)")
        if ($m.Count -gt 0) {
            $se = [int]$m[$m.Count - 1].Groups[1].Value
            if ($se -ne 0) { $exitCode = $se }
        }
    }
}

Write-Host ""
if ($exitCode -ne 0) {
    Write-Host ("FAILED exit " + $exitCode) -ForegroundColor Red
    if (Test-Path $logPath) { Get-Content $logPath -Tail 40 }
} else {
    Write-Host "Stopped OK" -ForegroundColor Yellow
}

$null = Read-ConsoleLine -Prompt "Press Enter to close"
exit $exitCode
