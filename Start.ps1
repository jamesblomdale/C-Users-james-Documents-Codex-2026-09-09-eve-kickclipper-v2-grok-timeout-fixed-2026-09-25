$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.\venv\Scripts\python.exe')) { throw 'Run Setup.cmd first.' }
$listening = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 5059 -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', ('"{0}"' -f (Join-Path $PSScriptRoot 'Run-Server.cmd')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
    Start-Sleep -Seconds 3
}
Start-Process 'http://127.0.0.1:5059/dashboard'
