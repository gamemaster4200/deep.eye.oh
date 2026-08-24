[CmdletBinding()]
param(
    [switch]$UpdateVendor
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$extensionPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'extension'))
$updateScript = Join-Path $PSScriptRoot 'update-vendor.ps1'
$validateScript = Join-Path $PSScriptRoot 'validate.ps1'

function Invoke-Step {
    param(
        [string]$Name,
        [string]$Script
    )

    Write-Host "[$Name]"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Script
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE."
    }
}

try {
    if ($UpdateVendor) {
        Invoke-Step -Name '1/2 EXPLICIT VENDOR UPDATE' -Script $updateScript
    }
    else {
        Write-Host '[1/2 Vendor] using current pinned files; no network query or download'
    }
    Invoke-Step -Name '2/2 Validate implementation' -Script $validateScript

    Write-Host ''
    Write-Host 'Validation passed. This script does not launch, close, or otherwise manage any Chrome process or profile.'
    Write-Host 'Finish the reload yourself in your normal Chrome:'
    Write-Host '  1. Open chrome://extensions'
    Write-Host '  2. First time only: enable Developer mode, click Load unpacked, and select:'
    Write-Host "       $extensionPath"
    Write-Host "  3. Click Reload on 'deep.eye.oh Browser Oracle'"
    Write-Host '  4. Reload https://diep.io/'
    exit 0
}
catch {
    Write-Error "Development refresh failed: $($_.Exception.Message)"
    exit 1
}
