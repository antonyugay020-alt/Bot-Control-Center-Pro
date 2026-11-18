# BCC VELES coin-capital: isolated automated native Windows acceptance.
# WIP source only; no live bots, exchange orders, GitHub Actions or paid API.
# Single parent folder: Desktop\BCC_VELES_COIN_QA\{SOURCE,RUN_<SHA8>}.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$source = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$parent = Split-Path $source -Parent
$expectedBranch = 'codex/veles-coin-capital-policy-20261118'
if ((Split-Path $source -Leaf) -ne 'SOURCE' -or
    (Split-Path $parent -Leaf) -ne 'BCC_VELES_COIN_QA') {
    throw 'BLOCKED: expected independent Desktop\BCC_VELES_COIN_QA\SOURCE'
}
if (-not (Test-Path -LiteralPath (Join-Path $source '.git'))) {
    throw 'BLOCKED: not a Git checkout'
}
# Reject Windows junctions/symlinks to another workspace. A seemingly
# isolated QA checkout must not be a reparse-point alias to live bots.
foreach ($p in @($source, $parent, (Join-Path $source '.git'))) {
    if ((Get-Item -LiteralPath $p -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) {
        throw 'BLOCKED: source, parent or Git directory is a reparse point'
    }
}
$branchName = ((& git -C $source branch --show-current) -join '').Trim()
$branchOK = ($LASTEXITCODE -eq 0)
$sha = ((& git -C $source rev-parse HEAD) -join '').Trim()
$shaOK = ($LASTEXITCODE -eq 0)
$modified = @(& git -C $source status --porcelain --untracked-files=normal)
$statusOK = ($LASTEXITCODE -eq 0)
if (-not $branchOK -or -not $shaOK -or -not $statusOK -or
    $branchName -ne $expectedBranch -or $sha -notmatch '^[0-9a-f]{40}$' -or
    @($modified | Where-Object { $_ -ne '' }).Count -gt 0) {
    throw 'BLOCKED: checkout must be clean and on exact feature branch'
}
if (Test-Path -LiteralPath (Join-Path $source 'MY_BOTS')) {
    throw 'BLOCKED: live trading account directory detected'
}
$short = $sha.Substring(0, 8)
$run = Join-Path $parent ('RUN_' + $short)
if (Test-Path -LiteralPath $run) {
    throw 'BLOCKED: this source SHA already has an acceptance workspace'
}
New-Item -ItemType Directory -Path $run -Force | Out-Null
$checkpoint = 'BCC_VELES_COIN_' + $short
$package = Join-Path $run 'SEALED_SOURCE.zip'
$checkParent = Join-Path $run 'CLEAN_UNPACK'
$check = Join-Path $checkParent $checkpoint
$console = Join-Path $run 'QA_CONSOLE.txt'
$summaryPath = Join-Path $run 'QA_SUMMARY.json'
$automatedZip = Join-Path $run 'AUTOMATED_WINDOWS_RESULTS.zip'
$reportZip = Join-Path $run 'LATEST_REPORT.zip'
$started = (Get-Date).ToUniversalTime().ToString('o')
$exitCode = 2
$result = 'BLOCKED_NOT_RELEASE'
$python = $null
$prefix = @()
foreach ($candidate in @('py', 'python')) {
    if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
    $probe = @()
    if ($candidate -eq 'py') { $probe += '-3' }
    $probe += @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)')
    & $candidate @probe 2>$null
    if ($LASTEXITCODE -eq 0) {
        $python = (Get-Command $candidate).Source
        if ($candidate -eq 'py') { $prefix = @('-3') }
        break
    }
}
try {
    if (-not $python) { throw 'Python 3.10+ was not found' }
    Push-Location $source
    try {
        $sealArgs = @('-m','tools.seal_source','--root',$source,
            '--output',$package,'--checkpoint',$checkpoint)
        & $python @prefix @sealArgs
        if ($LASTEXITCODE -ne 0) { throw 'Immutable source sealing failed' }
    } finally { Pop-Location }
    New-Item -ItemType Directory -Path $checkParent -Force | Out-Null
    Expand-Archive -LiteralPath $package -DestinationPath $checkParent
    if (-not (Test-Path -LiteralPath (Join-Path $check 'SHA256_MANIFEST.json'))) {
        throw 'Sealed source unpack missing manifest'
    }
    Push-Location $check
    try {
        # Existing full Bybit automated checker provisions a private venv
        # inside CLEAN_UNPACK and runs partitioned native WS/Qt/PAPER QA.
        $script = Join-Path $check 'tools\start_windows_check.ps1'
        $ErrorActionPreference = 'Continue'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Venue bybit -AutomatedOnly 2>&1 | Tee-Object -FilePath $console
        $exitCode = [int]$LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = 'Stop'
    }
    $reports = @(Get-ChildItem -LiteralPath $check -Filter 'BCC_VALIDATION_RESULTS_*.zip' -File -ErrorAction SilentlyContinue)
    if ($reports.Count -ne 1) { throw 'Expected exactly one full native QA report ZIP' }
    Copy-Item -LiteralPath $reports[0].FullName -Destination $automatedZip
    $result = if ($exitCode -eq 0) {
        'AUTOMATED_WINDOWS_PASS_MANUAL_PENDING_NOT_RELEASE'
    } else {
        'AUTOMATED_WINDOWS_FAILED_NOT_RELEASE'
    }
} catch {
    $result = 'BLOCKED_NOT_RELEASE'
    if ($exitCode -eq 0) { $exitCode = 2 }
} finally {
    $summary = [ordered]@{
        status = $result
        source_sha = $sha
        source_branch = $branchName
        checkpoint = $checkpoint
        started_utc = $started
        finished_utc = (Get-Date).ToUniversalTime().ToString('o')
        exit_code = $exitCode
        automated_bybit = $true
        manual_qt_operator_e2e = 'NOT_RUN'
        other_venues = 'NOT_RUN'
        trading_accounts_modified_by_wrapper = $false
        real_orders_authorized = $false
        github_actions_requested = 0
        paid_model_requests = 0
        release_approved = $false
    }
    $summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
    $parts = @($summaryPath)
    if (Test-Path -LiteralPath $console) { $parts += $console }
    if (Test-Path -LiteralPath $automatedZip) { $parts += $automatedZip }
    if (Test-Path -LiteralPath $package) { $parts += $package }
    Compress-Archive -LiteralPath $parts -DestinationPath $reportZip -Force
    Write-Host "BCC VELES COIN QA: $result"
    Write-Host "Source SHA: $sha"
    Write-Host "Single QA archive: $reportZip"
    Write-Host 'No product release or manual Qt E2E claim.'
}
exit $exitCode
