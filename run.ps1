# Popup Stopper bootstrap: creates the virtual environment, installs
# dependencies, and starts the app.
#
# Usage:
#   .\run.ps1                 -> set up (if needed) and run
#   .\run.ps1 -InstallIcon    -> also build the icon and Desktop shortcut
#   .\run.ps1 -Setup          -> set up only, do not launch

param(
    [switch]$InstallIcon,
    [switch]$Setup
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $here

$venv = Join-Path $here '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "Creating the virtual environment in .venv ..."
    py -3 -m venv $venv
}

Write-Host "Checking dependencies ..."
& $python -m pip install --disable-pip-version-check --quiet --upgrade pip
& $python -m pip install --disable-pip-version-check --quiet -r (Join-Path $here 'requirements.txt')

if ($InstallIcon) {
    Write-Host "Building the icon and installing shortcuts ..."
    & $python (Join-Path $here 'scripts\install_shortcut.py')
}

if ($Setup) {
    Write-Host "Setup complete. Launch with .\run.ps1 or the Desktop shortcut."
    exit 0
}

Write-Host "Starting Popup Stopper (Windows will ask for administrator rights) ..."
& $pythonw -m popupstopper
