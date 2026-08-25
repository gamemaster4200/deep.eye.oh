[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$extensionRoot = Join-Path $repoRoot 'extension'
$manifestPath = Join-Path $extensionRoot 'manifest.json'
$vendorPath = Join-Path $extensionRoot 'vendor\diepAPI.user.js'
$lockPath = Join-Path $extensionRoot 'vendor\diepAPI.lock.json'

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Assert-ExtensionFile {
    param([string]$RelativePath)

    Assert-True (-not [string]::IsNullOrWhiteSpace($RelativePath)) 'Manifest contains an empty file reference.'
    $fullPath = [System.IO.Path]::GetFullPath((Join-Path $extensionRoot $RelativePath))
    # DirectorySeparatorChar (not a hardcoded '\') so this also works under
    # pwsh on a non-Windows dev machine -- identical behavior on Windows,
    # where it's still '\'.
    $sep = [System.IO.Path]::DirectorySeparatorChar
    $rootPrefix = [System.IO.Path]::GetFullPath($extensionRoot).TrimEnd($sep) + $sep
    Assert-True ($fullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) `
        "Manifest file reference escapes the extension root: $RelativePath"
    Assert-True (Test-Path -LiteralPath $fullPath -PathType Leaf) `
        "Manifest-referenced file is missing: $RelativePath"
}

try {
    Assert-True (Test-Path -LiteralPath $manifestPath -PathType Leaf) "Manifest is missing: $manifestPath"
    $manifestText = Get-Content -Raw -LiteralPath $manifestPath
    try {
        $manifest = $manifestText | ConvertFrom-Json
    }
    catch {
        throw "Manifest JSON does not parse: $($_.Exception.Message)"
    }

    Assert-True ($manifest.manifest_version -eq 3) 'manifest_version must be 3.'
    Assert-True ($manifestText -notmatch '<all_urls>') 'Manifest must not contain <all_urls>.'
    $allowedPermissions = @('clipboardWrite', 'scripting')
    $unexpectedPermissions = @($manifest.permissions | Where-Object { $_ -notin $allowedPermissions })
    Assert-True ($unexpectedPermissions.Count -eq 0) `
        "Manifest has unexpected permissions: $($unexpectedPermissions -join ', ')"
    Assert-True (@($manifest.permissions).Count -eq $allowedPermissions.Count) `
        'Manifest must contain exactly the two reviewed permissions.'
    foreach ($permission in $allowedPermissions) {
        Assert-True ($permission -in @($manifest.permissions)) "Required permission is missing: $permission"
    }

    $allowedPattern = 'https://diep.io/*'
    $scopes = @()
    $scopes += @($manifest.host_permissions)
    $allowedWorlds = @('MAIN', 'ISOLATED')
    foreach ($contentScript in @($manifest.content_scripts)) {
        $scopes += @($contentScript.matches)
        $scopes += @($contentScript.exclude_matches)
        Assert-True ($contentScript.world -in $allowedWorlds) 'Every content script must explicitly use world MAIN or ISOLATED.'
        foreach ($script in @($contentScript.js)) {
            Assert-ExtensionFile $script
        }
        if ($null -ne $contentScript.css) {
            foreach ($style in @($contentScript.css)) {
                Assert-ExtensionFile $style
            }
        }
    }
    foreach ($scope in $scopes) {
        if (-not [string]::IsNullOrWhiteSpace($scope)) {
            Assert-True ($scope -eq $allowedPattern) "Host scope is not limited to diep.io: $scope"
        }
    }
    Assert-True (@($manifest.host_permissions).Count -eq 1) 'Exactly one host permission is expected.'
    # browser-overlay-control-v0: exactly two content script declarations
    # are now expected -- the original MAIN-world Oracle (read-only,
    # page-context, no chrome.* access) and one ISOLATED-world overlay
    # (chrome.runtime access, never touches window.deepEyeOracle/diepAPI).
    Assert-True (@($manifest.content_scripts).Count -eq 2) 'Exactly two content script declarations are expected (Oracle + overlay).'
    $mainContentScripts = @($manifest.content_scripts | Where-Object { $_.world -eq 'MAIN' })
    $isolatedContentScripts = @($manifest.content_scripts | Where-Object { $_.world -eq 'ISOLATED' })
    Assert-True ($mainContentScripts.Count -eq 1) 'Exactly one MAIN-world content script is expected.'
    Assert-True ($isolatedContentScripts.Count -eq 1) 'Exactly one ISOLATED-world content script is expected.'
    $mainScripts = @($mainContentScripts[0].js)
    Assert-True ($mainScripts.Count -eq 1) 'Exactly one MAIN-world script is expected.'
    Assert-True ($mainScripts[0] -eq 'src/oracle.js') `
        'The oracle must be the only MAIN-world runtime script; the pinned vendor must not be manifest-loaded.'
    $isolatedScripts = @($isolatedContentScripts[0].js)
    Assert-True ($isolatedScripts.Count -eq 1) 'Exactly one ISOLATED-world script is expected.'
    Assert-True ($isolatedScripts[0] -eq 'src/overlay.js') `
        'The overlay must be the only ISOLATED-world runtime script.'
    # browser-informed-farming-v0: exactly one background service worker is
    # now expected -- the reviewed Oracle-snapshot-to-localhost bridge, and
    # nothing else (no extra background keys, e.g. no persistent page).
    Assert-True ($null -ne $manifest.background) 'A background service worker is expected (the Oracle bridge).'
    Assert-True ($manifest.background.service_worker -eq 'background/bridge.js') `
        'The background service worker must be exactly background/bridge.js.'
    $backgroundKeys = @($manifest.background.PSObject.Properties.Name)
    Assert-True ($backgroundKeys.Count -eq 1 -and $backgroundKeys[0] -eq 'service_worker') `
        'The background block must declare only service_worker.'

    if ($null -ne $manifest.action -and $null -ne $manifest.action.default_popup) {
        Assert-ExtensionFile $manifest.action.default_popup
    }
    Assert-ExtensionFile $manifest.background.service_worker
    if ($null -ne $manifest.icons) {
        foreach ($property in $manifest.icons.PSObject.Properties) {
            Assert-ExtensionFile ([string]$property.Value)
        }
    }
    if ($null -ne $manifest.web_accessible_resources) {
        foreach ($resourceGroup in @($manifest.web_accessible_resources)) {
            foreach ($resource in @($resourceGroup.resources)) {
                Assert-ExtensionFile $resource
            }
        }
    }

    Assert-True (Test-Path -LiteralPath $vendorPath -PathType Leaf) 'Vendored diepAPI provenance file is missing.'
    $vendor = Get-Item -LiteralPath $vendorPath
    Assert-True ($vendor.Length -ge 10000) "Vendored diepAPI is unexpectedly small ($($vendor.Length) bytes)."

    Assert-True (Test-Path -LiteralPath $lockPath -PathType Leaf) 'Vendor lock is missing.'
    try {
        $lock = Get-Content -Raw -LiteralPath $lockPath | ConvertFrom-Json
    }
    catch {
        throw "Vendor lock JSON does not parse: $($_.Exception.Message)"
    }
    Assert-True ($lock.source_repo -eq 'https://github.com/Cazka/diepAPI') 'Vendor lock has unexpected provenance.'
    Assert-True ($lock.license -eq 'MIT') 'Vendor lock must identify the MIT license.'
    Assert-True ($lock.asset_name -eq 'diepAPI.user.js') 'Vendor lock has an unexpected asset name.'
    Assert-True (([string]$lock.sha256) -match '^[0-9a-fA-F]{64}$') 'Vendor lock SHA-256 is invalid.'
    $actualSha256 = (Get-FileHash -LiteralPath $vendorPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-True (([string]$lock.sha256).ToLowerInvariant() -eq $actualSha256) `
        "Vendor lock SHA-256 does not match diepAPI.user.js ($actualSha256)."

    # oracle.js, popup.js, and overlay.js must never touch the network or
    # send game-control primitives -- WebSocket included, since any of
    # them doing so would mean this "read-only" runtime is quietly doing
    # more than observing/rendering UI. overlay.js relays exclusively
    # through a chrome.runtime.connect port to background/bridge.js (the
    # one reviewed exception below), never a WebSocket of its own.
    $runtimeSources = @(
        (Join-Path $extensionRoot 'src\oracle.js'),
        (Join-Path $extensionRoot 'popup\popup.js'),
        (Join-Path $extensionRoot 'src\overlay.js')
    )
    $gameControlPatterns = @(
        '\bspawn\s*\(',
        '\bmoveTo\s*\(',
        '\baimAt\s*\(',
        '\blookAt\s*\(',
        '\bshoot\s*\(',
        '\bkeyDown\s*\(',
        '\bkeyUp\s*\(',
        '\bkeyPress\s*\(',
        '\bmouse(?:Press)?\s*\(',
        '\buseGamepad\s*\(',
        '\bupgrade_(?:stat|tank)\s*\(',
        '\bset_convar\s*\(',
        '\binput\.execute\s*\('
    )
    $forbiddenRuntimePatterns = @('\bWebSocket\b') + $gameControlPatterns
    foreach ($runtimeSource in $runtimeSources) {
        $runtimeText = Get-Content -Raw -LiteralPath $runtimeSource
        foreach ($pattern in $forbiddenRuntimePatterns) {
            Assert-True ($runtimeText -notmatch $pattern) `
                "Read-only boundary violation in $runtimeSource (pattern: $pattern)"
        }
    }

    # background/bridge.js is the one reviewed exception to the WebSocket
    # ban above (its entire job is exporting Oracle snapshots over one
    # local WebSocket) but it must still never contain a game-control
    # primitive, a hand-crafted protocol send, or a WebSocket.prototype
    # patch -- it forwards oracle.js's own snapshot() output outward only,
    # never anything inbound into the page or the game.
    $bridgePath = Join-Path $extensionRoot 'background\bridge.js'
    Assert-True (Test-Path -LiteralPath $bridgePath -PathType Leaf) 'The Oracle bridge is missing: background/bridge.js.'
    $bridgeText = Get-Content -Raw -LiteralPath $bridgePath
    foreach ($pattern in ($gameControlPatterns + @('\.send\(([''"])', 'WebSocket\.prototype'))) {
        Assert-True ($bridgeText -notmatch $pattern) `
            "Read-only boundary violation in background/bridge.js (pattern: $pattern)"
    }
    Assert-True ($bridgeText -match '\bnew\s+WebSocket\s*\(') `
        'background/bridge.js must use a plain WebSocket client (not found).'

    $extensionJavaScript = @(Get-ChildItem -LiteralPath $extensionRoot -Recurse -File -Filter '*.js')
    foreach ($scriptFile in $extensionJavaScript) {
        $scriptText = Get-Content -Raw -LiteralPath $scriptFile.FullName
        Assert-True ($scriptText -notmatch '\beval\s*\(') `
            "Executable eval is forbidden: $($scriptFile.FullName)"
        Assert-True ($scriptText -notmatch '\bnew\s+Function\s*\(') `
            "Dynamic Function construction is forbidden: $($scriptFile.FullName)"
    }

    $tracked = & git -C $repoRoot ls-files
    Assert-True ($LASTEXITCODE -eq 0) 'git ls-files failed.'
    $forbiddenTracked = @($tracked | Where-Object {
        $_ -match '(^|/)(\.chrome[^/]*profile|captures|recordings|browser-profiles?)(/|$)' `
            -or $_ -match '\.har(\.txt)?$' `
            -or $_ -match '(^|/)(cookies?|tokens?|auth-material|websocket[-_]?tickets?)([._/-]|$)' `
            -or $_ -match '(^|/)\.env(\.|$)'
    })
    Assert-True ($forbiddenTracked.Count -eq 0) `
        "Sensitive capture/profile/auth artifacts are tracked: $($forbiddenTracked -join ', ')"

    foreach ($powerShellScript in @(Get-ChildItem -LiteralPath $PSScriptRoot -File -Filter '*.ps1')) {
        $parseTokens = $null
        $parseErrors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile(
            $powerShellScript.FullName,
            [ref]$parseTokens,
            [ref]$parseErrors
        )
        Assert-True ($parseErrors.Count -eq 0) `
            "PowerShell parse failed for $($powerShellScript.FullName): $($parseErrors.Message -join '; ')"
    }

    if (-not $SkipTests) {
        $node = Get-Command node -ErrorAction SilentlyContinue
        Assert-True ($null -ne $node) 'Node.js is required to run the lightweight JavaScript checks.'
        $overlayPath = Join-Path $extensionRoot 'src\overlay.js'
        foreach ($script in @(
            $vendorPath,
            (Join-Path $extensionRoot 'src\oracle.js'),
            (Join-Path $extensionRoot 'popup\popup.js'),
            $bridgePath,
            $overlayPath,
            (Join-Path $repoRoot 'tests\oracle.test.js'),
            (Join-Path $repoRoot 'tests\repository.test.js'),
            (Join-Path $repoRoot 'tests\bridge.test.js'),
            (Join-Path $repoRoot 'tests\overlay.test.js')
        )) {
            & $node.Source --check $script
            Assert-True ($LASTEXITCODE -eq 0) "JavaScript syntax check failed: $script"
        }
        & $node.Source (Join-Path $repoRoot 'tests\oracle.test.js')
        Assert-True ($LASTEXITCODE -eq 0) 'Oracle tests failed.'
        & $node.Source (Join-Path $repoRoot 'tests\repository.test.js')
        Assert-True ($LASTEXITCODE -eq 0) 'Repository tests failed.'
        & $node.Source (Join-Path $repoRoot 'tests\bridge.test.js')
        Assert-True ($LASTEXITCODE -eq 0) 'Bridge tests failed.'
        & $node.Source (Join-Path $repoRoot 'tests\overlay.test.js')
        Assert-True ($LASTEXITCODE -eq 0) 'Overlay tests failed.'
    }

    Write-Host 'Validation passed:'
    Write-Host '  Manifest: MV3, ordered MAIN world, diep.io-only scope, exact minimal permissions'
    Write-Host "  Vendor: $($vendor.Length) bytes, SHA-256 $actualSha256"
    Write-Host '  Git: no tracked HAR/profile/auth artifacts detected'
    Write-Host '  Boundary: owned page/popup runtime has no control/WebSocket primitives; background bridge has no control primitives; extension has no eval'
    Write-Host '  PowerShell: parser checks passed'
    if (-not $SkipTests) {
        Write-Host '  JavaScript: syntax and oracle behavior checks passed'
    }
    exit 0
}
catch {
    Write-Error "Validation failed: $($_.Exception.Message)"
    exit 1
}
