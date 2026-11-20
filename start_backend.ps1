# 所有 daemon 由后端 lifespan 统一管理。
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing .venv; prepare dependencies with /install"
}
& $python -m app.main
exit $LASTEXITCODE
