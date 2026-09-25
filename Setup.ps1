$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.\venv\Scripts\python.exe')) {
    py -3.12 -m venv venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
}
& '.\venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. The application has not been started.' }
& '.\venv\Scripts\python.exe' -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed.' }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw 'Install FFmpeg and add it to PATH.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env'; Write-Host 'Set the provider key and owner login in .env before starting.' }
Write-Host 'Setup complete. Run Start.ps1 to open the local app.'
