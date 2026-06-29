# =========================================================================
#  Briefa - One-click setup (PowerShell)
#
#  Shared template across Lingora / Tella / Briefa. Only the CONFIG block
#  below differs per project; core logic identical.
#
#  ASCII-only because PowerShell 5.1 (Win10/11 default) reads .ps1 as ANSI
#  when there's no UTF-8 BOM. Viet co dau gets mangled.
# =========================================================================

# ─── CONFIG (per-project) ────────────────────────────────────────────────
$PROJECT_NAME    = "Briefa"
$PROJECT_TAGLINE = "Factual briefing video (URL/repo/text -> MP4 + citation)."
$MIN_PYTHON_MAJ  = 3
$MIN_PYTHON_MIN  = 11
$REQUIRES_NODE   = $true
$MIN_NODE        = 22
$DEPS_INSTALL    = @('-r', 'requirements.txt')
$ENV_TEMPLATE    = ".env.example"
$ENV_DEST        = ".env"
$ENV_KEY_LINE    = "GEMINI_API_KEY"
$RUN_HINT        = "Double-click run.bat de tao video briefing."
$POST_HINTS = @(
    "1. Mo $ENV_DEST (Notepad), dien $ENV_KEY_LINE.",
    "   - Lay key mien phi tai: https://aistudio.google.com/apikey",
    "2. (Khuyen nghi) Dien CLOUDFLARE_ACCOUNTS de scene co anh dep.",
    "   - Khong co cung chay duoc - layout fallback gradient.",
    "3. $RUN_HINT"
)

# ─── CORE (shared — do not edit per project) ─────────────────────────────
$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$bannerLine = "=" * 73
Write-Host ""
Write-Host $bannerLine -ForegroundColor Cyan
Write-Host "  $PROJECT_NAME - Setup" -ForegroundColor Cyan
Write-Host "  $PROJECT_TAGLINE" -ForegroundColor Gray
Write-Host $bannerLine -ForegroundColor Cyan
Write-Host ""

$totalSteps = 4
if ($REQUIRES_NODE) { $totalSteps++ }
$script:step = 0
function NextStep([string]$label) {
    $script:step++
    Write-Host "[$script:step/$script:totalSteps] $label" -ForegroundColor Yellow
}

$hasError = $false

# --- Check Python --------------------------------------------------------
NextStep "Kiem tra Python..."
$pythonExe = $null
foreach ($cand in @('python', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $verRaw = (& $cand -c "import sys; print(f'{sys.version_info[0]} {sys.version_info[1]}')" 2>$null)
    if (-not $verRaw) { continue }
    $parts = ($verRaw -join ' ').Trim() -split '\s+'
    $major = [int]$parts[0]; $minor = [int]$parts[1]
    if ($major -gt $MIN_PYTHON_MAJ -or ($major -eq $MIN_PYTHON_MAJ -and $minor -ge $MIN_PYTHON_MIN)) {
        $pythonExe = $cmd.Source
        break
    }
}
if (-not $pythonExe) {
    Write-Host "    [X] Python $MIN_PYTHON_MAJ.$MIN_PYTHON_MIN+ chua cai." -ForegroundColor Red
    Write-Host "        Tai tai:  https://www.python.org/downloads/" -ForegroundColor Gray
    Write-Host "        Khi cai: TICK 'Add Python to PATH' o buoc dau tien." -ForegroundColor Gray
    $hasError = $true
} else {
    $pyV = (& $pythonExe --version 2>&1) -join ' '
    Write-Host "    [OK] $pyV" -ForegroundColor Green
}

# --- Check Node (skipped if not required) --------------------------------
if ($REQUIRES_NODE) {
    NextStep "Kiem tra Node.js..."
    $nodeCmd = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCmd) {
        Write-Host "    [X] Node.js $MIN_NODE+ chua cai." -ForegroundColor Red
        Write-Host "        Tai Node LTS tai:  https://nodejs.org/en/download" -ForegroundColor Gray
        $hasError = $true
    } else {
        $nodeVRaw = (& node --version 2>&1) -join ''
        $nodeMajor = [int](($nodeVRaw -replace '^v', '') -split '\.')[0]
        if ($nodeMajor -lt $MIN_NODE) {
            Write-Host "    [X] Node.js $nodeVRaw - can $MIN_NODE+." -ForegroundColor Red
            $hasError = $true
        } else {
            Write-Host "    [OK] Node.js $nodeVRaw" -ForegroundColor Green
        }
    }
}

# --- Check ffmpeg (auto-download portable if missing) --------------------
NextStep "Kiem tra ffmpeg..."
$ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
$localFfmpeg = Join-Path $PSScriptRoot 'bin\ffmpeg\bin\ffmpeg.exe'
if ($ffmpegCmd) {
    Write-Host "    [OK] ffmpeg tren PATH" -ForegroundColor Green
} elseif (Test-Path $localFfmpeg) {
    Write-Host "    [OK] ffmpeg local da co o bin\ffmpeg\bin\" -ForegroundColor Green
} else {
    Write-Host "    [!] ffmpeg chua cai. Tool se tu tai ban portable (~80 MB)..." -ForegroundColor Yellow
    try {
        $binDir = Join-Path $PSScriptRoot 'bin'
        if (-not (Test-Path $binDir)) { New-Item -ItemType Directory -Path $binDir | Out-Null }
        $zipPath = Join-Path $binDir 'ffmpeg.zip'
        Write-Host "        Dang tai ffmpeg-release-essentials.zip ..." -ForegroundColor Gray
        Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $zipPath -UseBasicParsing
        Write-Host "        Dang giai nen ..." -ForegroundColor Gray
        $extractDir = Join-Path $binDir 'ffmpeg-extracted'
        if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
        $extracted = Get-ChildItem -Directory -Path $extractDir | Where-Object { $_.Name -like 'ffmpeg-*' } | Select-Object -First 1
        if (-not $extracted) { throw "Khong thay folder ffmpeg-* trong zip" }
        $targetDir = Join-Path $binDir 'ffmpeg'
        if (Test-Path $targetDir) { Remove-Item -Recurse -Force $targetDir }
        Move-Item -Path $extracted.FullName -Destination $targetDir
        Remove-Item -Recurse -Force $extractDir
        Remove-Item -Force $zipPath
        if (Test-Path $localFfmpeg) {
            Write-Host "    [OK] ffmpeg portable cai tai bin\ffmpeg\bin\ffmpeg.exe" -ForegroundColor Green
        } else {
            throw "ffmpeg.exe khong thay sau khi giai nen"
        }
    } catch {
        Write-Host "    [X] Auto-download that bai: $_" -ForegroundColor Red
        Write-Host "        Tai tay:  https://www.gyan.dev/ffmpeg/builds/" -ForegroundColor Gray
        Write-Host "        Chon 'ffmpeg-release-essentials.zip' - giai nen vao bin\ffmpeg\" -ForegroundColor Gray
        $hasError = $true
    }
}

if ($hasError) {
    Write-Host ""
    Write-Host $bannerLine -ForegroundColor Red
    Write-Host "  Thieu cong cu. Cai theo huong dan tren roi chay lai SETUP.bat." -ForegroundColor Red
    Write-Host $bannerLine -ForegroundColor Red
    Write-Host ""
    Read-Host "Nhan Enter de thoat"
    exit 1
}

# --- Create venv + install Python deps -----------------------------------
NextStep "Tao Python venv va cai thu vien..."
$venvDir = Join-Path $PSScriptRoot '.venv'
if (-not (Test-Path $venvDir)) {
    & $pythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    [X] Khong tao duoc venv (Python thieu module venv?)" -ForegroundColor Red
        Read-Host "Nhan Enter de thoat"; exit 1
    }
    Write-Host "    [OK] Tao .venv\ xong." -ForegroundColor Green
} else {
    Write-Host "    [OK] .venv\ da ton tai, skip create." -ForegroundColor Green
}

$venvPython = Join-Path $venvDir 'Scripts\python.exe'
Write-Host "    Dang upgrade pip..." -ForegroundColor Gray
& $venvPython -m pip install --upgrade pip --quiet
Write-Host "    Dang cai thu vien (co the mat 1-3 phut)..." -ForegroundColor Gray
$pipArgs = @('-m', 'pip', 'install') + $DEPS_INSTALL
& $venvPython @pipArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "    [X] pip install loi. Xem log o tren." -ForegroundColor Red
    Read-Host "Nhan Enter de thoat"; exit 1
}
Write-Host "    [OK] Python deps da cai xong." -ForegroundColor Green

# --- Initialize .env -----------------------------------------------------
NextStep "Khoi tao file cau hinh..."
$envPath = Join-Path $PSScriptRoot $ENV_DEST
$envExample = Join-Path $PSScriptRoot $ENV_TEMPLATE
if (-not (Test-Path $envPath)) {
    if (Test-Path $envExample) {
        $envParent = Split-Path -Parent $envPath
        if ($envParent -and -not (Test-Path $envParent)) {
            New-Item -ItemType Directory -Path $envParent -Force | Out-Null
        }
        Copy-Item -Path $envExample -Destination $envPath
        Write-Host "    [OK] Da tao $ENV_DEST tu $ENV_TEMPLATE" -ForegroundColor Green
    } else {
        Write-Host "    [!] Khong thay $ENV_TEMPLATE - ban can tao $ENV_DEST thu cong." -ForegroundColor Yellow
    }
} else {
    Write-Host "    [OK] $ENV_DEST da co san, giu nguyen." -ForegroundColor Green
}

# --- Done ----------------------------------------------------------------
Write-Host ""
Write-Host $bannerLine -ForegroundColor Green
Write-Host "  [OK] SETUP XONG." -ForegroundColor Green
Write-Host ""
Write-Host "  Buoc tiep theo:" -ForegroundColor White
foreach ($hint in $POST_HINTS) {
    Write-Host "    $hint" -ForegroundColor White
}
Write-Host $bannerLine -ForegroundColor Green
Write-Host ""
Read-Host "Nhan Enter de thoat"
