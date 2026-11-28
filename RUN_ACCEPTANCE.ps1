param([string]$ReportRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$ReportRoot = [System.IO.Path]::GetFullPath($ReportRoot)
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($env:OS -ne 'Windows_NT') { throw 'Run this package on native Windows.' }
    $python = $null
    $prefix = @()
    foreach ($name in @('py', 'python')) {
        $candidate = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if (-not $candidate) { continue }
        $probePrefix = @()
        if ($name -eq 'py') { $probePrefix = @('-3') }
        & $candidate[0].Source @probePrefix -I -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
        if ($LASTEXITCODE -eq 0) {
            $python = $candidate[0].Source
            $prefix = $probePrefix
            break
        }
    }
    if (-not $python) { throw 'Python 3.10+ was not found. Install Python and run the script again.' }
    # Ignore any PYTHONPATH, user site packages, or Python startup hooks from another installation.
    & $python @prefix -E -s -m tools.windows_v39_acceptance --report-root $ReportRoot
    exit $LASTEXITCODE
} catch {
    $stem = 'V39_ACCEPTANCE_WINDOWS_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fffffff')
    $folder = Join-Path $ReportRoot $stem
    New-Item -ItemType Directory -Path $folder | Out-Null
    @{
        mode = 'Windows'
        source_integrity_pass = $false
        acceptance_exit = 1
        release_ready = $false
        production_deployed = $false
        error = $_.Exception.Message
    } | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $folder 'BOOTSTRAP_SUMMARY.json')
    $archive = $folder + '.zip'
    Compress-Archive -LiteralPath (Join-Path $folder 'BOOTSTRAP_SUMMARY.json') -DestinationPath $archive
    [Console]::Error.WriteLine($_.Exception.Message)
    Write-Host ('Report: ' + $archive)
    exit 1
} finally {
    Pop-Location
}
