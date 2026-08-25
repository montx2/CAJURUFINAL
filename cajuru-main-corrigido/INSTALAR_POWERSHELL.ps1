# Requires -Version 5.1
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
Write-Host 'Cajuru A1: instalacao pelo PowerShell.' -ForegroundColor Cyan
Write-Host 'O Dropbox nao sera alterado; selecione somente CERTIFICADOS/CERTIFICADOS A1.' -ForegroundColor Yellow
& "$PSScriptRoot\INSTALAR.bat"
exit $LASTEXITCODE
