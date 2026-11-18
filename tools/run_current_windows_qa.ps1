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
if ($LASTEXITCODE -ne 0) { throw 'Automated QA failed. Send the BCC_VALIDATION_RESULTS ZIP from the QA folder.' }
Write-Host 'Automated checks finished. Starting the independent manual PAPER session.'
& '.\.check_venv\Scripts\python.exe' '.\tools\manual_qa_current.py' $root $Commit
if ($LASTEXITCODE -ne 0) { throw 'Manual QA did not finish; preserve the verification_reports folder.' }
