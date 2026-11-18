# Native Windows Bybit PAPER QA: sealed independent source only.
# No GitHub Actions, paid LLMs, reboot, or stop/cleanup of live bots.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '.'))
$expected = 'codex/coin-budget-unified-20261118'
$git = Get-Command git -ErrorAction Stop
$branch = ((& git -C $repo branch --show-current) -join '').Trim()
if ($LASTEXITCODE -ne 0 -or $branch -ne $expected) { throw 'BLOCKED: incorrect branch' }
$sha = ((& git -C $repo rev-parse HEAD) -join '').Trim()
if ($LASTEXITCODE -ne 0 -or $sha -notmatch '^[0-9a-f]{40}$') { throw 'BLOCKED: invalid HEAD' }
$dirty = @(& git -C $repo status --porcelain --untracked-files=normal)
if ($LASTEXITCODE -ne 0 -or @($dirty | Where-Object { $_ -ne '' }).Count -gt 0) {
    throw 'BLOCKED: source checkout is dirty. Use a clean isolated branch.'
}
if (-not $env:USERPROFILE) { throw 'BLOCKED: Windows user profile unavailable' }
$qa = Join-Path (Join-Path $env:USERPROFILE 'Desktop') ('BCC_COIN_BUDGET_QA_' + $sha.Substring(0,12))
if (Test-Path -LiteralPath $qa) {
    throw 'BLOCKED: QA destination already exists. Do not overwrite or reuse prior artifacts.'
}
$stage = Join-Path $qa 'sealed_source'
$archive = Join-Path $stage 'BCC_SOURCE_SNAPSHOT.zip'
$unpack = Join-Path $stage 'unpacked'
$rootName = 'BCC_BYBIT_COIN_BUDGET_' + $sha.Substring(0,12)
$package = Join-Path $unpack $rootName
New-Item -ItemType Directory -Path $stage -Force | Out-Null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = 'py'; $prefix = @('-3','-B')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = 'python'; $prefix = @('-B')
} else { throw 'BLOCKED: Python 3 unavailable' }
Push-Location $repo
try {
    & $python @prefix -m tools.seal_source --root $repo --output $archive --checkpoint $rootName
    if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: source sealing or ZIP roundtrip failed' }
} finally { Pop-Location }
New-Item -ItemType Directory -Path $unpack | Out-Null
Expand-Archive -LiteralPath $archive -DestinationPath $unpack
if (-not (Test-Path -LiteralPath (Join-Path $package 'CHECK_BYBIT_WINDOWS.bat'))) {
    throw 'BLOCKED: expected sealed CHECK_BYBIT_WINDOWS.bat was not extracted'
}
Write-Host 'Sealed Bybit source verified in a new independent Windows folder.'
Write-Host 'Starting native AUTOMATED_ONLY QA: complete suite + public WS + Qt.'
Write-Host 'No real orders; this does not run manual acceptance on live accounts.'
$exitCode = 98
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
        (Join-Path $package 'tools\start_windows_check.ps1') -Venue bybit -AutomatedOnly
    $exitCode = [int]$LASTEXITCODE
} catch {
    Write-Host 'BLOCKED: isolated Windows QA bootstrap did not complete.'
    $exitCode = 98
}
$report = Get-ChildItem -LiteralPath $package -Filter 'BCC_VALIDATION_RESULTS_*.zip' -File |
    Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if ($null -ne $report) {
    $out = Join-Path $qa 'LATEST_REPORT.zip'
    Copy-Item -LiteralPath $report.FullName -Destination $out
    Write-Host "Single result archive: $out"
} else {
    Write-Host 'BLOCKED: no sealed QA report archive was produced.'
    if ($exitCode -eq 0) { $exitCode = 97 }
}
Write-Host "Checked source SHA: $sha"
Write-Host "Native Windows QA exit code: $exitCode"
Write-Host 'Release gate remains closed until all manual and other mandatory acceptance checks pass.'
exit $exitCode
