# Flowboard one-click start for Windows (no make / WSL needed).
#   powershell -ExecutionPolicy Bypass -File start.ps1
# First run installs dependencies, then starts the agent (:8434) and the UI (:5173).
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$agent = Join-Path $root 'agent'
$frontend = Join-Path $root 'frontend'
$py = Join-Path $agent '.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host 'Setting up the agent (first run)...'
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Push-Location $agent
        uv venv --python 3.11
        uv pip install --python $py -e .
        Pop-Location
    } else {
        Push-Location $agent
        python -m venv .venv
        & $py -m pip install -e .
        Pop-Location
    }
}
if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
    Write-Host 'Installing frontend packages (first run)...'
    Push-Location $frontend; npm install; Pop-Location
}

Start-Process -WorkingDirectory $agent -FilePath $py -ArgumentList '-m','uvicorn','flowboard.main:app','--port','8434'
Start-Process -WorkingDirectory $frontend -FilePath 'npm.cmd' -ArgumentList 'run','dev'
Start-Sleep -Seconds 4
Start-Process 'http://localhost:5173/?studio=1'

Write-Host ''
Write-Host 'Flowboard is starting.'
Write-Host '  1. In Chrome open chrome://extensions, enable Developer mode, Load unpacked -> the "extension" folder.'
Write-Host '  2. Sign in at https://flow.google.com and keep that tab open.'
Write-Host '  3. Use the Studio page that just opened.'
