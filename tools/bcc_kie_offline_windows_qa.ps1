# BCC Kie.ai development-only offline Windows acceptance.
# No model APIs, GitHub Actions, downloads, trading programs, process stops, reboot.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$expectedBranch = 'agent/bcc-work-claude-gateway-20261117'
if ((Split-Path -Path $root -Leaf) -ne 'BCC_AGENT_GATEWAY') {
    throw 'BLOCKED: only the isolated BCC_AGENT_GATEWAY directory is permitted'
}
if (-not (Test-Path -LiteralPath (Join-Path $root '.git'))) {
    throw 'BLOCKED: no independent source checkout found'
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'BLOCKED: Git was not found'
}
$branch = ((& git -C $root branch --show-current) -join '').Trim()
if ($LASTEXITCODE -ne 0 -or $branch -ne $expectedBranch) {
    throw 'BLOCKED: incorrect Git branch'
}
$statusLines = @(& git -C $root status --porcelain --untracked-files=normal)
if ($LASTEXITCODE -ne 0 -or @($statusLines | Where-Object { $_ -ne '' }).Count -ne 0) {
    throw 'BLOCKED: checkout contains source modifications or untracked files'
}
$sha = ((& git -C $root rev-parse HEAD) -join '').Trim()
if ($LASTEXITCODE -ne 0 -or $sha -notmatch '^[0-9a-f]{40}$') {
    throw 'BLOCKED: cannot pin a valid source SHA'
}
$runner = Join-Path $root 'tools\bcc_kie_offline_acceptance.py'
if (-not (Test-Path -LiteralPath $runner)) {
    throw 'BLOCKED: offline suite runner missing'
}
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = (Get-Command py).Source
    $pyArgs = @('-3','-I','-B',$runner)
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = (Get-Command python).Source
    $pyArgs = @('-I','-B',$runner)
} else {
    throw 'BLOCKED: Python 3 was not found'
}
$qaDir = Join-Path $root 'QA'
New-Item -ItemType Directory -Path $qaDir -Force | Out-Null
$log = Join-Path $qaDir 'BCC_KIE_QA_CONSOLE.txt'
$summaryPath = Join-Path $qaDir 'BCC_KIE_QA_SUMMARY.json'
$archive = Join-Path $qaDir 'BCC_KIE_QA_RESULT.zip'
foreach ($file in @($log,$summaryPath,$archive)) {
    if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
}
$started = (Get-Date).ToUniversalTime().ToString('o')
$oldBytecode = $env:PYTHONDONTWRITEBYTECODE
$oldUserSite = $env:PYTHONNOUSERSITE
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONNOUSERSITE = '1'
$exitCode = 98
Push-Location $root
try {
    # Child process executes only Kie offline tests; socket/subprocess
    # operations are blocked in Python before any test module is imported.
    $ErrorActionPreference = 'Continue'
    & $python @pyArgs 2>&1 | Tee-Object -FilePath $log
    $exitCode = [int]$LASTEXITCODE
} catch {
    'BLOCKED: unexpected offline QA runner failure' |
        Out-File -FilePath $log -Append -Encoding UTF8
    $exitCode = 98
} finally {
    Pop-Location
    $env:PYTHONDONTWRITEBYTECODE = $oldBytecode
    $env:PYTHONNOUSERSITE = $oldUserSite
    $ErrorActionPreference = 'Stop'
}
$body = if (Test-Path -LiteralPath $log) {
    Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue
} else { '' }
$passed = ($exitCode -eq 0 -and $body -match 'BCC_KIE_OFFLINE_RESULT=.*"result": "PASS"')
if (-not $passed -and $exitCode -eq 0) { $exitCode = 97 }
$state = if ($passed) { 'OFFLINE_PASS_ONLY' } else { 'OFFLINE_BLOCKED_OR_FAIL' }
$summary = [ordered]@{
    check = 'BCC_KIE_OFFLINE_QA'
    result = $state
    source_branch = $branch
    source_sha = $sha
    started_utc = $started
    completed_utc = (Get-Date).ToUniversalTime().ToString('o')
    exit_code = $exitCode
    test_scope = 'test_bcc_kie_*.py'
    actual_windows_qt_e2e = $false
    trading_processes_modified = $false
    paid_provider_calls = 0
    github_actions_requested = 0
    live_model_compatibility = 'UNVERIFIED'
    release_approved = $false
}
$summary | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath $summaryPath -Encoding UTF8
Compress-Archive -LiteralPath @($log, $summaryPath) -DestinationPath $archive -Force
Write-Host ''
Write-Host "BCC KIE QA: $state"
Write-Host "Source SHA: $sha"
Write-Host "Single QA archive: $archive"
Write-Host 'This result is not a full Windows Qt E2E or a live Kie API test.'
exit $exitCode
