param([switch]$AutomatedOnly, [ValidateSet('all', 'bybit')][string]$Venue = 'all')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
try {
    $python = $null
    $prefix = @()
    foreach ($candidate in @('py', 'python')) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        $probeArgs = @()
        if ($candidate -eq 'py') { $probeArgs += '-3' }
        $probeArgs += @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)')
        & $candidate @probeArgs 2>$null
        if ($LASTEXITCODE -eq 0) {
            $python = $candidate
            if ($candidate -eq 'py') { $prefix = @('-3') }
            break
        }
    }
    if (-not $python) { throw 'Python 3.10+ was not found. Install Python 3.12 with Python Launcher, then run CHECK_WINDOWS.bat again.' }
    $runArgs = @('tools\windows_check.py', '--venue', $Venue)
    if ($AutomatedOnly) { $runArgs += '--automated-only' }
    & $python @prefix @runArgs
    exit $LASTEXITCODE
} catch {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $dir = Join-Path (Get-Location) ('verification_reports\bootstrap_' + $stamp)
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $_.ToString() | Set-Content -Encoding UTF8 (Join-Path $dir 'bootstrap_error.txt')
    $archive = Join-Path (Get-Location) ('BCC_VALIDATION_RESULTS_' + $stamp + '_bootstrap.zip')
    Compress-Archive -Path (Join-Path $dir '*') -DestinationPath $archive -Force
    Write-Host $_.ToString()
    Write-Host ('Report: ' + $archive)
    exit 1
}
