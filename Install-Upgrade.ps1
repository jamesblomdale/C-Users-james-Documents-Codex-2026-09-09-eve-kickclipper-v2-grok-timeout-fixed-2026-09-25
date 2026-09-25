param([string]$Destination = 'C:\Users\james\kick-clipper')
$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$targetRoot = [System.IO.Path]::GetFullPath($Destination)
if ($sourceRoot -eq $targetRoot) { throw 'Run this installer from the separate extracted upgrade folder.' }
if (-not (Test-Path -LiteralPath (Join-Path $targetRoot 'app.py'))) { throw 'Choose the existing kick-clipper project folder.' }
$socket = New-Object System.Net.Sockets.TcpClient
try { $socket.Connect('127.0.0.1',5000); throw 'The existing app is running. Stop it and its processing jobs before installing this upgrade.' }
catch [System.Net.Sockets.SocketException] { }
finally { $socket.Dispose() }
$backupRoot = Join-Path $targetRoot ('upgrade-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
$files = Get-ChildItem -LiteralPath $sourceRoot -Recurse -File | Where-Object {
    $_.Name -ne '.env' -and $_.Extension -ne '.db' -and $_.Extension -ne '.pyc' -and
    $_.FullName -notmatch '[\\/](venv|output|__pycache__|upgrade-backups)[\\/]'
}
foreach ($file in $files) {
    $relative = $file.FullName.Substring($sourceRoot.Length).TrimStart('\','/')
    $target = [System.IO.Path]::GetFullPath((Join-Path $targetRoot $relative))
    if (-not $target.StartsWith($targetRoot.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe installation path.' }
    if (Test-Path -LiteralPath $target) {
        $backup = Join-Path $backupRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backup) | Out-Null
        Copy-Item -LiteralPath $target -Destination $backup
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $target -Force
}
Write-Host "Upgrade installed. Previous source files: $backupRoot"
Write-Host 'Your .env, account database, model cache and output clips have been preserved.'
Write-Host 'Use Start.ps1 in the original folder. For a new environment, run Setup.ps1 first.'
