param([Parameter(Mandatory=$true)][string]$Commit)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root
if ((Split-Path $root -Leaf) -cnotmatch '^BCC_NATIVE_QA_CURRENT_[a-zA-Z0-9_]+$') {
    throw 'Refusing a non-dedicated QA folder.'
}
if ($Commit -cnotmatch '^[0-9a-f]{40}$') { throw 'A full lowercase commit SHA is required.' }
$head = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $head -cne $Commit) { throw 'Checkout does not match the requested commit.' }
if (Test-Path -LiteralPath '.check_venv') { throw 'This runner requires a new, unused checkout.' }
if (Test-Path -LiteralPath 'check_workspaces') { throw 'Existing QA accounts are not adopted.' }
if (Test-Path -LiteralPath 'verification_reports') { throw 'Existing reports are not adopted.' }
Write-Host 'Running isolated Windows automated QA. No manual acceptance is implied.'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File '.\tools\start_windows_check.ps1' -AutomatedOnly -Venue all
$qaExit = $LASTEXITCODE
if (-not (Test-Path -LiteralPath '.\.check_venv\Scripts\python.exe')) {
    throw 'QA environment was not created. Send the BCC_VALIDATION_RESULTS ZIP from the QA folder.'
}
Write-Host 'Generating the automatic checklist and public WebSocket evidence. No questions or manual bot actions.'
& '.\.check_venv\Scripts\python.exe' -m tools.auto_qa_current $root $Commit
$reportExit = $LASTEXITCODE
if ($qaExit -ne 0 -or $reportExit -ne 0) {
    throw 'Automatic QA has failures or unconfirmed evidence. Send the BCC_TEST_REPORT_AUTOMATED ZIP and, if present, BCC_VALIDATION_RESULTS ZIP.'
}
Write-Host 'Automated checks finished. Full live PAPER acceptance is not implied.'
