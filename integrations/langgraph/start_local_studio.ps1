[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
$studioEnv = Join-Path $scriptDir ".env.studio"
$safeExample = Join-Path $scriptDir ".env.example"
$langgraph = Join-Path $repoRoot ".venv\Scripts\langgraph.exe"

if (-not (Test-Path -LiteralPath $studioEnv)) {
    if (-not (Test-Path -LiteralPath $safeExample)) {
        throw "Studio environment example is missing."
    }
    Copy-Item -LiteralPath $safeExample -Destination $studioEnv
    Write-Host "Created integrations/langgraph/.env.studio for the local test profile."
}
if (-not (Test-Path -LiteralPath $langgraph)) {
    throw "Root virtualenv LangGraph CLI is missing."
}

Push-Location $scriptDir
try {
    & $langgraph dev --config langgraph.json --host 127.0.0.1 --port 2024 --no-browser --no-reload
    if ($LASTEXITCODE -ne 0) {
        throw "LangGraph Studio process exited with an error."
    }
}
finally {
    Pop-Location
}
