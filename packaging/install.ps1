#Requires -Version 5.1
<#
.SYNOPSIS
    Installs deep.eye.oh: per-user, no Administrator privileges, no
    pre-installed Python, no Git required.

.DESCRIPTION
    1. Ensures `uv` is available (bootstraps it deterministically if not).
    2. Resolves and downloads the latest release's wheel from GitHub
       (deep.eye.oh is a public repo, so this is a plain anonymous
       download -- no credentials needed anywhere in this script).
    3. Installs/reinstalls deep-eye-oh as an isolated `uv tool`.
    4. Ensures the tool's executable directory is on your PATH.
    5. Runs `deep-eye-oh doctor` as a lightweight install verification.

    Safe to rerun: this script is idempotent (a second run reinstalls in
    place rather than erroring).

    Chrome for Testing itself is NOT downloaded by this script -- it is
    fetched automatically, once, the first time you run
    `deep-eye-oh browser-farm`.
#>

$ErrorActionPreference = "Stop"

# Some older/clean Windows 10 installs still negotiate TLS 1.0 by default
# under Windows PowerShell 5.1, which breaks HTTPS calls to GitHub/astral.sh
# with an opaque "underlying connection was closed" error -- force TLS 1.2.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = "gamemaster4200/deep.eye.oh"
$AppDataRoot = Join-Path $env:LOCALAPPDATA "deep-eye-oh"
$UvInstallDir = Join-Path $AppDataRoot "uv"

function Write-Step($Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

# --- 1. Ensure uv is available, bootstrapped deterministically -------------
#
# Installing uv does not guarantee THIS PowerShell process immediately
# re-resolves it on PATH, so: pin a known install directory ourselves
# (UV_INSTALL_DIR, an officially supported uv installer setting) and invoke
# uv.exe by that explicit, known path for the rest of this script -- never
# assume the current process inherited a PATH change.

$existingUv = Get-Command uv -ErrorAction SilentlyContinue
if ($existingUv) {
    $uvExe = $existingUv.Source
    Write-Step "uv already available at $uvExe"
} else {
    Write-Step "uv not found; installing to $UvInstallDir"
    New-Item -ItemType Directory -Force -Path $UvInstallDir | Out-Null
    $env:UV_INSTALL_DIR = $UvInstallDir
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $uvExe = Join-Path $UvInstallDir "uv.exe"
    if (-not (Test-Path $uvExe)) {
        throw "uv installation did not produce $uvExe -- see the astral.sh/uv install output above."
    }
}

# --- 2. Resolve the latest release wheel via the GitHub API ----------------
#
# Anonymous and public: deep.eye.oh is a public repo, so this needs no
# token/credentials. The wheel filename encodes the version, so we look it
# up rather than assuming/constructing it.

Write-Step "looking up the latest deep.eye.oh release"
$release = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$Repo/releases/latest"
$asset = $release.assets | Where-Object { $_.name -like "deep_eye_oh-*-py3-none-any.whl" } | Select-Object -First 1
if (-not $asset) {
    throw "no deep_eye_oh-*-py3-none-any.whl asset found on the latest GitHub release of $Repo"
}

$downloadDir = Join-Path $env:TEMP "deep-eye-oh-install"
New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
$whlPath = Join-Path $downloadDir $asset.name
Write-Step "downloading $($asset.name) ($($release.tag_name))"
Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $whlPath

# --- 3. Install/reinstall as an isolated uv tool ----------------------------

Write-Step "installing deep-eye-oh via uv tool"
& $uvExe tool install $whlPath --force
if ($LASTEXITCODE -ne 0) {
    throw "uv tool install failed (exit code $LASTEXITCODE)"
}

# --- 4. Resolve the installed executable's directory programmatically ------

$binDir = (& $uvExe tool dir --bin | Select-Object -First 1).Trim()
$exePath = Join-Path $binDir "deep-eye-oh.exe"
if (-not (Test-Path $exePath)) {
    throw "uv tool install reported success but $exePath does not exist"
}

# --- 5. Lightweight verification, by full path -- no PATH dependency -------

Write-Step "verifying the install"
& $exePath doctor
$doctorExit = $LASTEXITCODE

# --- 6. Ensure future shells have the tool bin dir on PATH ------------------
#
# uv tool install does not touch PATH by itself. An already-configured PATH
# is success, not a no-op failure -- this process does not need to (and
# will not) see the change itself.

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$alreadyOnPath = $false
if ($userPath) {
    $alreadyOnPath = ($userPath -split ";") | Where-Object { $_.TrimEnd('\') -ieq $binDir.TrimEnd('\') } | Select-Object -First 1
}
if ($alreadyOnPath) {
    Write-Step "PATH already includes $binDir"
} else {
    Write-Step "adding $binDir to your user PATH"
    & $uvExe tool update-shell
}

# --- 7. Done -----------------------------------------------------------------

Write-Host ""
if ($doctorExit -ne 0) {
    Write-Host "deep.eye.oh installed, but 'doctor' reported a problem above -- run 'deep-eye-oh doctor' after opening a new terminal for details." -ForegroundColor Yellow
} else {
    Write-Host "deep.eye.oh installed and ready." -ForegroundColor Green
}
Write-Host ""
Write-Host "Open a NEW terminal and run:"
Write-Host "    deep-eye-oh browser-farm" -ForegroundColor Cyan
