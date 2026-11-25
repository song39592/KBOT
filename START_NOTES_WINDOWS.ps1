$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$core = Join-Path $PSScriptRoot "HKU_CoLearning_KBot_Formal_v8.0.13"
$python = Join-Path $PSScriptRoot ".notes-venv/Scripts/python.exe"
if (!(Test-Path $python)) {
    py -3.11 -m venv .notes-venv
    if ($LASTEXITCODE -ne 0) { throw "Install Python 3.11 and retry." }
    & $python -m pip install -r "$core/requirements.txt"
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
}
& $python "$core/run_notes_windows.py"
