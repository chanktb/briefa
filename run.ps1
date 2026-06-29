# =========================================================================
#  Briefa - Interactive wizard (PowerShell)
#
#  N-input wizard cho briefa CLI. Khong sua engine - chi assemble args
#  goi `python -m briefa`, sau do restructure output thanh 1 folder phang.
#
#  Flow:
#    1. Inputs   (loop: URL / Text / Image, mix tu do, max 10)
#    2. Aspect   (9:16 / 16:9)
#    3. Length   (short / detailed)
#    4. Channel  (pick tu list HOAC skip)
#       - Neu pick: hoi theme override + language override
#       - Neu skip: pick language + theme bat buoc (map sang fallback channel)
#    5. Image hint (chi hoi khi co image input)
#    6. Confirm + render
#    7. Restructure: gom moi thu vao output/<job_name>/
#
#  NOTE: ASCII-only (PowerShell 5.1 mac dinh tren Windows doc .ps1 ANSI
#  khong co BOM). Khong dung tieng Viet co dau trong CODE file nay
#  (Display name doc tu channel.env UTF8 thi van OK).
# =========================================================================

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location -Path $PSScriptRoot

# Pre-cleanup: xoa stale jobs/ va output/ tu run truoc (neu file bi lock
# luc do, gio may da release). Dam bao snapshot dung.
function Remove-WithRetry {
    param(
        [string]$Path,
        [int]$MaxAttempts = 5,
        [int]$DelayMs = 1000
    )
    if (-not (Test-Path $Path)) { return $true }
    for ($i = 1; $i -le $MaxAttempts; $i++) {
        try {
            Remove-Item -Path $Path -Recurse -Force -ErrorAction Stop
            return $true
        } catch {
            if ($i -lt $MaxAttempts) {
                Start-Sleep -Milliseconds $DelayMs
            }
        }
    }
    return $false
}

foreach ($staleRoot in @('jobs','output')) {
    $p = Join-Path $PSScriptRoot $staleRoot
    if (Test-Path $p) {
        # chi xoa khi rong hoac chi co file rac - giu data nguoi dung neu co
        $items = Get-ChildItem -Path $p -Force -ErrorAction SilentlyContinue
        if (-not $items) {
            Remove-WithRetry -Path $p | Out-Null
        }
    }
}

# --- Activate venv -------------------------------------------------------
$venvActivate = Join-Path $PSScriptRoot '.venv\Scripts\Activate.ps1'
if (Test-Path $venvActivate) {
    . $venvActivate
}

# --- Resolve python ------------------------------------------------------
$pythonExe = $null
foreach ($cand in @('python', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $pythonExe = $cmd.Source; break }
}
if (-not $pythonExe) {
    Write-Host "Khong tim thay python tren PATH. Cai Python 3.11+ truoc." -ForegroundColor Red
    Read-Host "Nhan Enter de thoat"
    exit 1
}

Write-Host ""
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  Briefa - Tao briefing video tu URL / Text / Image" -ForegroundColor Cyan
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host ""

# ═════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════

function ConvertTo-Slug {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return 'job' }
    # Strip diacritics: NFD + remove non-spacing marks
    $normalized = $Text.Normalize([Text.NormalizationForm]::FormD)
    $sb = New-Object Text.StringBuilder
    foreach ($c in $normalized.ToCharArray()) {
        if ([Globalization.CharUnicodeInfo]::GetUnicodeCategory($c) -ne [Globalization.UnicodeCategory]::NonSpacingMark) {
            [void]$sb.Append($c)
        }
    }
    $s = $sb.ToString().ToLower()
    $s = $s -replace '[^a-z0-9]+', '-'
    $s = $s.Trim('-')
    if ([string]::IsNullOrWhiteSpace($s)) { return 'job' }
    if ($s.Length -gt 40) { $s = $s.Substring(0, 40).TrimEnd('-') }
    return $s
}

function Read-ChannelMeta {
    param([string]$ChannelDir)
    $envFile = Join-Path $ChannelDir 'channel.env'
    $slug = Split-Path $ChannelDir -Leaf
    $meta = [PSCustomObject]@{
        Slug        = $slug
        DisplayName = $slug   # fallback if CHANNEL_NAME missing
        Theme       = 'dark'
        Voice       = ''
        Language    = 'unknown'
    }
    if (-not (Test-Path $envFile)) { return $meta }
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        if ($line -match '^\s*#') { continue }
        if ($line -match '^\s*CHANNEL_NAME\s*=\s*(.+?)\s*$') {
            $meta.DisplayName = $matches[1].Trim()
        } elseif ($line -match '^\s*THEME_VARIANT\s*=\s*(.+?)\s*$') {
            $meta.Theme = $matches[1].Trim()
        } elseif ($line -match '^\s*VOICE_NAME\s*=\s*(.+?)\s*$') {
            $meta.Voice = $matches[1].Trim()
            if ($meta.Voice -match '^([a-z]{2}-[A-Z]{2})') {
                $meta.Language = $matches[1]
            }
        }
    }
    return $meta
}

function Get-AllChannels {
    $root = Join-Path $PSScriptRoot 'channels'
    if (-not (Test-Path $root)) { return @() }
    $dirs = Get-ChildItem -Path $root -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName 'channel.env') } |
        Sort-Object Name
    $result = @()
    foreach ($d in $dirs) {
        $result += Read-ChannelMeta -ChannelDir $d.FullName
    }
    return $result
}

# ═════════════════════════════════════════════════════════════════════════
# Step 1: Collect N inputs (URL / Text / Image, mix freely)
# ═════════════════════════════════════════════════════════════════════════

Write-Host "[1/5] Nhap nguon du lieu" -ForegroundColor Yellow
Write-Host "      Briefa nhan N nguon (URL bao + URL GitHub repo + text + anh)" -ForegroundColor Gray
Write-Host "      Toi da 10 nguon, trong do toi da 5 anh." -ForegroundColor Gray
Write-Host ""

$cliArgs = @('-m', 'briefa')
$inputCount = 0
$imageCount = 0
$hasImage = $false

while ($inputCount -lt 10) {
    Write-Host ("--- Nguon #{0} ---" -f ($inputCount + 1)) -ForegroundColor Cyan
    Write-Host "   [1] URL  (bai bao / GitHub repo)"
    Write-Host "   [2] Text (paragraph / ghi chu)"
    Write-Host ("   [3] Image (file path, da co {0}/5 anh)" -f $imageCount)
    if ($inputCount -ge 1) {
        Write-Host "   [Enter]  Da du, chuyen buoc tiep theo"
    }
    $kind = (Read-Host "Chon loai").Trim()

    if ([string]::IsNullOrWhiteSpace($kind)) {
        if ($inputCount -ge 1) { break }
        Write-Host "Can it nhat 1 nguon. Thu lai." -ForegroundColor Yellow
        continue
    }

    switch ($kind) {
        '1' {
            $url = $null
            while (-not $url) {
                $raw = (Read-Host "Paste URL").Trim()
                if ($raw -match '^https?://') { $url = $raw }
                else { Write-Host "URL phai bat dau http:// hoac https://" -ForegroundColor Yellow }
            }
            $cliArgs += @('--input', $url)
            $inputCount++
        }
        '2' {
            Write-Host "Dan text (Enter 1 dong trong de ket thuc):" -ForegroundColor Gray
            $lines = @()
            while ($true) {
                $line = Read-Host
                if (-not $line) {
                    if ($lines.Count -ge 1) { break }
                    Write-Host "Can it nhat 1 dong." -ForegroundColor Yellow
                    continue
                }
                $lines += $line
            }
            $text = ($lines -join "`n").Trim()
            if ($text) {
                $cliArgs += @('--input', $text)
                $inputCount++
            }
        }
        '3' {
            if ($imageCount -ge 5) {
                Write-Host "Da dat toi da 5 anh." -ForegroundColor Yellow
                continue
            }
            $imgPath = $null
            while (-not $imgPath) {
                $raw = (Read-Host "Duong dan anh (PNG/JPG/WEBP/GIF/BMP)").Trim('"').Trim()
                if (-not $raw) { break }
                if (Test-Path $raw -PathType Leaf) {
                    $ext = [System.IO.Path]::GetExtension($raw).ToLower()
                    if ($ext -in @('.png','.jpg','.jpeg','.webp','.gif','.bmp')) {
                        $imgPath = (Resolve-Path $raw).Path
                    } else {
                        Write-Host "Dinh dang khong ho tro: $ext" -ForegroundColor Yellow
                    }
                } else {
                    Write-Host "File khong ton tai: $raw" -ForegroundColor Yellow
                }
            }
            if ($imgPath) {
                $cliArgs += @('--image', $imgPath)
                $inputCount++
                $imageCount++
                $hasImage = $true
            }
        }
        default {
            Write-Host "Lua chon khong hop le." -ForegroundColor Yellow
        }
    }
    Write-Host ""
}

Write-Host ("[OK] Da nhap {0} nguon ({1} anh)" -f $inputCount, $imageCount) -ForegroundColor Green
Write-Host ""

# ═════════════════════════════════════════════════════════════════════════
# Step 2: Aspect ratio
# ═════════════════════════════════════════════════════════════════════════

Write-Host "[2/5] Ti le khung hinh" -ForegroundColor Yellow
Write-Host "   [1] 9:16  (Reels / TikTok / Shorts)  [mac dinh]"
Write-Host "   [2] 16:9  (YouTube long-form)"
$aspectRaw = (Read-Host "Chon (Enter = 9:16)").Trim()
$aspect = '9:16'
if ($aspectRaw -eq '2') { $aspect = '16:9' }
$cliArgs += @('--aspect', $aspect)
Write-Host ("[OK] Aspect: {0}" -f $aspect) -ForegroundColor Green
Write-Host ""

# ═════════════════════════════════════════════════════════════════════════
# Step 3: Length
# ═════════════════════════════════════════════════════════════════════════

Write-Host "[3/5] Do dai video" -ForegroundColor Yellow
Write-Host "   [1] short    ~60-110s, 5-8 scene  [mac dinh, hop Reels/TikTok]"
Write-Host "   [2] detailed ~140-220s, 10-15 scene  [hop YouTube]"
$lengthRaw = (Read-Host "Chon (Enter = short)").Trim()
$length = 'short'
if ($lengthRaw -eq '2') { $length = 'detailed' }
$cliArgs += @('--length', $length)
Write-Host ("[OK] Length: {0}" -f $length) -ForegroundColor Green
Write-Host ""

# ═════════════════════════════════════════════════════════════════════════
# Step 4: Channel pick (optional) + Theme/Language override
# ═════════════════════════════════════════════════════════════════════════

Write-Host "[4/5] Channel (kenh)" -ForegroundColor Yellow
Write-Host "      Channel quy dinh branding + giong doc + theme mac dinh." -ForegroundColor Gray
Write-Host "      Co the SKIP de tu chon language + theme thu cong." -ForegroundColor Gray
Write-Host ""

$channels = Get-AllChannels
if ($channels.Count -eq 0) {
    Write-Host "Khong tim thay channel nao. Khong the render khi chua co channel." -ForegroundColor Red
    Read-Host "Nhan Enter de thoat"
    exit 1
}

Write-Host "   [0] SKIP - khong dung channel cu the (se hoi language + theme)"
for ($i = 0; $i -lt $channels.Count; $i++) {
    $ch = $channels[$i]
    Write-Host ("   [{0}] {1}  (lang={2}, theme={3})" -f ($i + 1), $ch.DisplayName, $ch.Language, $ch.Theme)
}

$chosenChannel = $null
while ($null -eq $chosenChannel) {
    $raw = (Read-Host ("Chon (0-{0})" -f $channels.Count)).Trim()
    if ($raw -match '^\d+$') {
        $idx = [int]$raw
        if ($idx -eq 0) {
            $chosenChannel = '__SKIP__'
        } elseif ($idx -ge 1 -and $idx -le $channels.Count) {
            $chosenChannel = $channels[$idx - 1]
        }
    }
    if ($null -eq $chosenChannel) {
        Write-Host "Lua chon khong hop le, thu lai." -ForegroundColor Yellow
    }
}
Write-Host ""

# ── Branch A: channel picked ─────────────────────────────────────────────
if ($chosenChannel -ne '__SKIP__') {
    Write-Host ("[OK] Channel: {0}" -f $chosenChannel.DisplayName) -ForegroundColor Green
    Write-Host ("     Theme mac dinh: {0}" -f $chosenChannel.Theme) -ForegroundColor Gray
    Write-Host ("     Language mac dinh: {0} ({1})" -f $chosenChannel.Language, $chosenChannel.Voice) -ForegroundColor Gray
    Write-Host ""

    # Theme override
    Write-Host "Doi theme khac?" -ForegroundColor Yellow
    Write-Host "   [Enter] Giu mac dinh tu channel"
    Write-Host "   [1] dark       [2] bright     [3] corporate"
    Write-Host "   [4] vivid      [5] default"
    $themeRaw = (Read-Host "Chon").Trim()
    $themeMap = @{ '1'='dark'; '2'='bright'; '3'='corporate'; '4'='vivid'; '5'='default' }
    if ($themeMap.ContainsKey($themeRaw)) {
        $cliArgs += @('--theme', $themeMap[$themeRaw])
        Write-Host ("[OK] Theme override: {0}" -f $themeMap[$themeRaw]) -ForegroundColor Green
    } else {
        Write-Host "[OK] Theme: giu mac dinh tu channel" -ForegroundColor Green
    }
    Write-Host ""

    # Language override (best-effort: switch sang channel khac trong cung language)
    Write-Host "Doi language khac?" -ForegroundColor Yellow
    Write-Host "   [Enter] Giu language cua channel"
    Write-Host "   [1] vi-VN (Tieng Viet)"
    Write-Host "   [2] en-US (English)"
    $langRaw = (Read-Host "Chon").Trim()
    $newLang = $null
    if ($langRaw -eq '1') { $newLang = 'vi-VN' }
    elseif ($langRaw -eq '2') { $newLang = 'en-US' }

    if ($newLang -and $newLang -ne $chosenChannel.Language) {
        $candidates = $channels | Where-Object { $_.Language -eq $newLang }
        if ($candidates.Count -eq 0) {
            Write-Host ("[!] Chua co channel nao cho {0}. Giu nguyen channel '{1}'." -f $newLang, $chosenChannel.DisplayName) -ForegroundColor Yellow
            Write-Host ("    De them, tao folder channels/<slug>/ voi channel.env co VOICE_NAME={0}-..." -f $newLang) -ForegroundColor Gray
        } else {
            Write-Host ("Channel co san cho {0}:" -f $newLang) -ForegroundColor Cyan
            for ($i = 0; $i -lt $candidates.Count; $i++) {
                Write-Host ("   [{0}] {1}" -f ($i + 1), $candidates[$i].DisplayName)
            }
            $pick = $null
            while ($null -eq $pick) {
                $r = (Read-Host ("Chon (1-{0})" -f $candidates.Count)).Trim()
                if ($r -match '^\d+$') {
                    $i = [int]$r
                    if ($i -ge 1 -and $i -le $candidates.Count) { $pick = $candidates[$i - 1] }
                }
                if ($null -eq $pick) { Write-Host "Lua chon khong hop le." -ForegroundColor Yellow }
            }
            $chosenChannel = $pick
            Write-Host ("[OK] Channel switched to: {0} (lang={1})" -f $chosenChannel.DisplayName, $chosenChannel.Language) -ForegroundColor Green
        }
    } elseif ($newLang) {
        Write-Host "[OK] Language: giu nguyen (giong language cua channel)" -ForegroundColor Green
    } else {
        Write-Host "[OK] Language: giu mac dinh tu channel" -ForegroundColor Green
    }
    Write-Host ""

    $cliArgs += @('--channel', $chosenChannel.Slug)
}

# ── Branch B: skipped channel ────────────────────────────────────────────
else {
    Write-Host "[OK] Khong gan channel - se hoi language + theme bat buoc." -ForegroundColor Green
    Write-Host ""

    # Language pick (bat buoc)
    Write-Host "Language?" -ForegroundColor Yellow
    Write-Host "   [1] vi-VN (Tieng Viet)  [mac dinh]"
    Write-Host "   [2] en-US (English)"
    $langRaw = (Read-Host "Chon (Enter = vi-VN)").Trim()
    $language = 'vi-VN'
    if ($langRaw -eq '2') { $language = 'en-US' }

    # Map language -> 1 channel khop (fallback chosen automatically)
    $candidates = $channels | Where-Object { $_.Language -eq $language }
    if ($candidates.Count -eq 0) {
        Write-Host ("[X] Chua co channel nao cho {0}. Khong the render." -f $language) -ForegroundColor Red
        Write-Host ("    Tao folder channels/<slug>/ voi VOICE_NAME={0}-... truoc." -f $language) -ForegroundColor Gray
        Read-Host "Nhan Enter de thoat"
        exit 1
    }
    $chosenChannel = $candidates[0]
    Write-Host ("[OK] Language: {0} -> dung channel '{1}' lam fallback" -f $language, $chosenChannel.DisplayName) -ForegroundColor Green
    Write-Host ""

    # Theme pick (bat buoc)
    Write-Host "Theme?" -ForegroundColor Yellow
    Write-Host "   [1] dark       [mac dinh, editorial tin tuc]"
    Write-Host "   [2] bright     [editorial light, lifestyle/learning]"
    Write-Host "   [3] corporate  [navy + cyan + amber, B2B]"
    Write-Host "   [4] vivid"
    $themeRaw = (Read-Host "Chon (Enter = dark)").Trim()
    $themeMap = @{ '1'='dark'; '2'='bright'; '3'='corporate'; '4'='vivid' }
    $theme = 'dark'
    if ($themeMap.ContainsKey($themeRaw)) { $theme = $themeMap[$themeRaw] }
    $cliArgs += @('--theme', $theme)
    Write-Host ("[OK] Theme: {0}" -f $theme) -ForegroundColor Green
    Write-Host ""

    $cliArgs += @('--channel', $chosenChannel.Slug)
}

# ═════════════════════════════════════════════════════════════════════════
# Step 5: Image hint (chi hoi khi co image input)
# ═════════════════════════════════════════════════════════════════════════

if ($hasImage) {
    Write-Host "[5/5] Caption / hint cho Gemini Vision (toi da 200 ky tu)" -ForegroundColor Yellow
    Write-Host "      Vi du: 'Tin sang nay ve ket qua bau cu'" -ForegroundColor Gray
    $hint = (Read-Host "Hint (Enter de bo qua)").Trim()
    if ($hint) {
        if ($hint.Length -gt 200) { $hint = $hint.Substring(0, 200) }
        $cliArgs += @('--image-hint', $hint)
        Write-Host "[OK] Hint da luu." -ForegroundColor Green
    } else {
        Write-Host "[OK] Khong dung hint." -ForegroundColor Green
    }
    Write-Host ""
}

# ═════════════════════════════════════════════════════════════════════════
# Confirm + render
# ═════════════════════════════════════════════════════════════════════════

Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  Tom tat" -ForegroundColor Cyan
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host (" Nguon:    {0} (trong do {1} anh)" -f $inputCount, $imageCount)
Write-Host (" Aspect:   {0}" -f $aspect)
Write-Host (" Length:   {0}" -f $length)
Write-Host (" Channel:  {0}" -f $chosenChannel.DisplayName)
$themeArgIdx = [Array]::IndexOf($cliArgs, '--theme')
if ($themeArgIdx -ge 0) {
    Write-Host (" Theme:    {0} (override)" -f $cliArgs[$themeArgIdx + 1])
} else {
    Write-Host (" Theme:    (mac dinh tu channel: {0})" -f $chosenChannel.Theme)
}
Write-Host "==========================================================="
Write-Host ""

$confirm = (Read-Host "Bat dau render? (Y/n)").Trim().ToLower()
if ($confirm -and $confirm -ne 'y' -and $confirm -ne 'yes') {
    Write-Host "Da huy." -ForegroundColor Yellow
    Read-Host "Nhan Enter de thoat"
    exit 0
}

# ═════════════════════════════════════════════════════════════════════════
# Snapshot existing output/ + jobs/ folders BEFORE render
# (de detect folder moi sau khi CLI xong)
# ═════════════════════════════════════════════════════════════════════════

$outputRoot = Join-Path $PSScriptRoot 'output'
$jobsRoot   = Join-Path $PSScriptRoot 'jobs'
$channelOutDir = Join-Path $outputRoot $chosenChannel.Slug

$existingArtifactDirs = @()
if (Test-Path $channelOutDir) {
    $existingArtifactDirs = Get-ChildItem -Path $channelOutDir -Directory -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName
}
$existingJobsDirs = @()
if (Test-Path $jobsRoot) {
    $existingJobsDirs = Get-ChildItem -Path $jobsRoot -Directory -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName
}

# Compose job name TRUOC khi run (chi timestamp - vi da nam trong channel folder)
$jobName = Get-Date -Format "yyyy-MM-dd_HHmmss"

# Destination: channels/<channel-slug>/output/<jobName>/
$channelRoot   = Join-Path (Join-Path $PSScriptRoot 'channels') $chosenChannel.Slug
$channelOutputRoot = Join-Path $channelRoot 'output'

Write-Host ""
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host ("  Dang render... (1-3 phut tuy do dai + so nguon)") -ForegroundColor Cyan
Write-Host ("  Job: {0}" -f $jobName) -ForegroundColor Gray
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host ""

& $pythonExe @cliArgs
$exitCode = $LASTEXITCODE

# ═════════════════════════════════════════════════════════════════════════
# Post-process: restructure output thanh 1 folder phang
# ═════════════════════════════════════════════════════════════════════════

if ($exitCode -eq 0) {
    Write-Host ""
    Write-Host "Dang gom output vao 1 folder..." -ForegroundColor Gray

    # Find new artifact folder (CLI tao under output/<channel.Slug>/<slug>/)
    $newArtifact = $null
    if (Test-Path $channelOutDir) {
        $newArtifact = Get-ChildItem -Path $channelOutDir -Directory -ErrorAction SilentlyContinue |
            Where-Object { $existingArtifactDirs -notcontains $_.FullName } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
    }

    # Find new jobs folder (CLI tao under jobs/<timestamp_topic>/)
    $newJobsDirs = @()
    if (Test-Path $jobsRoot) {
        $newJobsDirs = @(Get-ChildItem -Path $jobsRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { $existingJobsDirs -notcontains $_.FullName })
    }

    if ($null -eq $newArtifact) {
        Write-Host "[!] Khong tim thay artifact moi. Render co the chua tao file." -ForegroundColor Yellow
    } else {
        # Tao dich: channels/<slug>/output/<jobName>/
        if (-not (Test-Path $channelOutputRoot)) {
            New-Item -ItemType Directory -Path $channelOutputRoot -Force | Out-Null
        }
        $destDir = Join-Path $channelOutputRoot $jobName
        # Phong khi 2 lan render trong cung 1 giay (hiem): append counter
        $counter = 1
        while (Test-Path $destDir) {
            $destDir = Join-Path $channelOutputRoot ("{0}_{1}" -f $jobName, $counter)
            $counter++
        }
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null

        # Move main artifacts (output.mp4, caption.txt, manifest.json va bat ky file nao khac)
        # Retry vi co the HyperFrames con giu handle vai giay sau khi render xong.
        function Move-FileWithRetry {
            param([string]$Source, [string]$DestinationDir, [int]$MaxAttempts = 5, [int]$DelayMs = 2000)
            for ($i = 1; $i -le $MaxAttempts; $i++) {
                try {
                    Move-Item -Path $Source -Destination $DestinationDir -Force -ErrorAction Stop
                    return $true
                } catch {
                    if ($i -lt $MaxAttempts) {
                        Start-Sleep -Milliseconds $DelayMs
                    }
                }
            }
            return $false
        }

        Get-ChildItem -Path $newArtifact.FullName -File -ErrorAction SilentlyContinue | ForEach-Object {
            $ok = Move-FileWithRetry -Source $_.FullName -DestinationDir $destDir
            if (-not $ok) {
                Write-Host ("[!] Khong move duoc {0} (file con bi lock). Se thu lai lan sau." -f $_.Name) -ForegroundColor Yellow
            }
        }

        # Xoa intermediates (jobs/<timestamp>/) - khong giu de gon
        if ($newJobsDirs.Count -gt 0) {
            foreach ($jd in $newJobsDirs) {
                $ok = Remove-WithRetry -Path $jd.FullName
                if (-not $ok) {
                    Write-Host ("[!] Khong xoa duoc {0} (con bi lock). Se don sau o lan run ke tiep." -f $jd.Name) -ForegroundColor Yellow
                }
            }
        }

        # Cleanup: xoa folder rong tu top-level output/ + jobs/ (retry safe)
        Remove-WithRetry -Path $newArtifact.FullName | Out-Null
        Remove-WithRetry -Path $channelOutDir | Out-Null
        Remove-WithRetry -Path $outputRoot | Out-Null
        Remove-WithRetry -Path $jobsRoot | Out-Null

        Write-Host ("[OK] Job folder: channels\{0}\output\{1}\" -f $chosenChannel.Slug, $jobName) -ForegroundColor Green

        # Mo File Explorer luon de CEO xem ngay, khong phai tu paste path
        try {
            Start-Process explorer.exe -ArgumentList "`"$destDir`""
        } catch {
            # neu ko mo duoc thi thoi - path da in o tren
        }
    }
}

Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "=== HOAN TAT ===" -ForegroundColor Green
    Write-Host (" Channel : {0}" -f $chosenChannel.DisplayName) -ForegroundColor Green
    Write-Host (" Job     : {0}" -f $jobName) -ForegroundColor Green
    Write-Host (" File    : output.mp4 + caption.txt + manifest.json") -ForegroundColor Gray
    Write-Host (" Explorer da mo san folder cho anh.") -ForegroundColor Gray
} else {
    Write-Host ("=== Loi - exit code {0}. Xem log ben tren de fix. ===" -f $exitCode) -ForegroundColor Red
}
Write-Host ""
Read-Host "Nhan Enter de dong cua so"
