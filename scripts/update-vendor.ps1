[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Replace-Atomically {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        [System.IO.File]::Replace($Source, $Destination, $null)
    }
    else {
        [System.IO.File]::Move($Source, $Destination)
    }
}

function Lock-MatchesRelease {
    param(
        [object]$Lock,
        [object]$Release,
        [object]$Asset,
        [string]$Sha256
    )

    return $null -ne $Lock `
        -and $Lock.source_repo -eq 'https://github.com/Cazka/diepAPI' `
        -and $Lock.license -eq 'MIT' `
        -and $Lock.release_tag -eq $Release.tag_name `
        -and $Lock.release_url -eq $Release.html_url `
        -and $Lock.asset_name -eq $Asset.name `
        -and $Lock.asset_url -eq $Asset.browser_download_url `
        -and ([string]$Lock.sha256).ToLowerInvariant() -eq $Sha256
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$vendorDirectory = Join-Path $repoRoot 'extension\vendor'
$vendorPath = Join-Path $vendorDirectory 'diepAPI.user.js'
$lockPath = Join-Path $vendorDirectory 'diepAPI.lock.json'
$licensePath = Join-Path $repoRoot 'third_party\diepAPI-LICENSE.txt'
$tempVendorPath = Join-Path $vendorDirectory ('.diepAPI.{0}.download' -f [Guid]::NewGuid().ToString('N'))
$tempLockPath = Join-Path $vendorDirectory ('.diepAPI-lock.{0}.download' -f [Guid]::NewGuid().ToString('N'))

try {
    if (-not (Test-Path -LiteralPath $licensePath -PathType Leaf)) {
        throw "The vendored MIT license is missing: $licensePath"
    }

    if (-not ([Net.ServicePointManager]::SecurityProtocol -band [Net.SecurityProtocolType]::Tls12)) {
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    }

    Write-Host 'Querying the latest Cazka/diepAPI release...'
    $headers = @{
        Accept = 'application/vnd.github+json'
        'User-Agent' = 'deep-eye-oh-ext-vendor-updater'
    }
    $release = Invoke-RestMethod `
        -Uri 'https://api.github.com/repos/Cazka/diepAPI/releases/latest' `
        -Headers $headers `
        -Method Get `
        -UseBasicParsing
    $asset = @($release.assets) | Where-Object { $_.name -eq 'diepAPI.user.js' } | Select-Object -First 1
    if ($null -eq $asset -or [string]::IsNullOrWhiteSpace($asset.browser_download_url)) {
        throw "Release '$($release.tag_name)' has no diepAPI.user.js asset."
    }

    Write-Host "Downloading $($release.tag_name)/$($asset.name)..."
    Invoke-WebRequest `
        -Uri $asset.browser_download_url `
        -Headers $headers `
        -OutFile $tempVendorPath `
        -UseBasicParsing

    $download = Get-Item -LiteralPath $tempVendorPath
    if ($download.Length -lt 10000) {
        throw "Downloaded vendor asset is unexpectedly small ($($download.Length) bytes)."
    }
    $header = Get-Content -LiteralPath $tempVendorPath -TotalCount 20 | Out-String
    if ($header -notmatch '@name\s+diepAPI') {
        throw 'Downloaded asset does not look like the diepAPI userscript.'
    }

    $sha256 = (Get-FileHash -LiteralPath $tempVendorPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $currentSha256 = $null
    if (Test-Path -LiteralPath $vendorPath -PathType Leaf) {
        $currentSha256 = (Get-FileHash -LiteralPath $vendorPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }

    $existingLock = $null
    if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
        try {
            $existingLock = Get-Content -Raw -LiteralPath $lockPath | ConvertFrom-Json
        }
        catch {
            Write-Warning 'The existing vendor lock is invalid and will be replaced.'
        }
    }

    $vendorChanged = $currentSha256 -ne $sha256
    $lockChanged = -not (Lock-MatchesRelease $existingLock $release $asset $sha256)
    if (-not $vendorChanged -and -not $lockChanged) {
        Write-Host "diepAPI $($release.tag_name) is already current ($sha256)."
        exit 0
    }

    Write-Warning 'DEPENDENCY PIN WILL CHANGE: review entity mappings and diepAPI compatibility before using new oracle data.'

    if ($vendorChanged) {
        Replace-Atomically -Source $tempVendorPath -Destination $vendorPath
        Write-Host "Updated vendor atomically: $vendorPath"
    }
    else {
        Remove-Item -LiteralPath $tempVendorPath
        Write-Host 'Vendor bytes are unchanged; the vendor file was not rewritten.'
    }

    $lock = [ordered]@{
        source_repo = 'https://github.com/Cazka/diepAPI'
        license = 'MIT'
        release_tag = [string]$release.tag_name
        release_url = [string]$release.html_url
        asset_name = [string]$asset.name
        asset_url = [string]$asset.browser_download_url
        sha256 = $sha256
        fetched_at_utc = [DateTime]::UtcNow.ToString('o')
    }
    $lockJson = ($lock | ConvertTo-Json -Depth 3) + [Environment]::NewLine
    [System.IO.File]::WriteAllText(
        $tempLockPath,
        $lockJson,
        [System.Text.UTF8Encoding]::new($false)
    )
    Replace-Atomically -Source $tempLockPath -Destination $lockPath

    Write-Host "Vendor lock updated: $lockPath"
    Write-Host "SHA-256: $sha256"
    Write-Warning 'REVIEW REQUIRED: entity mappings are vendor-version-specific; update and rerun mapping/API tests.'
    exit 0
}
catch {
    Write-Error "Vendor update failed: $($_.Exception.Message)"
    exit 1
}
finally {
    foreach ($temporaryPath in @($tempVendorPath, $tempLockPath)) {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}
