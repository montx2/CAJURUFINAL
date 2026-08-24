# Requires -Version 5.1
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
Write-Host 'Cajuru A1: iniciando interface.' -ForegroundColor Cyan
Write-Host 'Escopo protegido: somente CERTIFICADOS/CERTIFICADOS A1.' -ForegroundColor Yellow
& "$PSScriptRoot\INICIAR.bat"
exit $LASTEXITCODE
