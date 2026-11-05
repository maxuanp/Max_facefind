# facefind UI launcher (PowerShell). ASCII only.
# Works from any working directory: it switches to the script folder first.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$root = (Get-Location).Path
Write-Host "Project directory: $root"
Write-Host "Starting facefind local UI ..."

$exe = $null
$prefix = @()
$venv = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venv) {
    $exe = $venv
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $exe = 'py'
    $prefix = @('-3')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $exe = (Get-Command python).Source
}

if (-not $exe) {
    Write-Host ""
    Write-Host "[ERROR] Python 3 not found."
    Write-Host "  Option 1: install Python 3.10+ and enable 'Add python.exe to PATH'."
    Write-Host "  Option 2: run  py -3 -m venv .venv  in this folder."
    Write-Host "  Then install deps:  python -m pip install -r requirements.txt"
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "Python: $exe $($prefix -join ' ')"
Write-Host "URL:    http://127.0.0.1:8765/"
Write-Host "A tray icon appears after start: open / restart / quit the service from it."
Write-Host "The browser opens automatically when the server is ready."
Write-Host "Use  --no-tray  to run without the tray host (old mode)."

& $exe @prefix -m facefind.supervisor @args
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    Write-Host "[ERROR] startup failed, exit code $code"
    Write-Host "  Check deps: $exe $($prefix -join ' ') -m pip install -r requirements.txt"
    Read-Host "Press Enter to close"
    exit $code
}
