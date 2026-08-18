param(
    [ValidateSet("Dev", "Build", "Typecheck", "Test")]
    [string]$Action = "Dev"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$frontendRoot = Join-Path $repositoryRoot "frontend"
$packageManifest = Join-Path $frontendRoot "package.json"
$nodeModules = Join-Path $frontendRoot "node_modules"

if (-not (Test-Path -LiteralPath $packageManifest)) {
    throw "Frontend package.json을 찾을 수 없습니다: $packageManifest"
}
if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
    throw "Node.js가 필요합니다. Node.js를 설치한 뒤 다시 실행하세요."
}
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw "npm.cmd를 찾을 수 없습니다. Node.js 설치 상태를 확인하세요."
}
if (-not (Test-Path -LiteralPath $nodeModules)) {
    throw "Frontend 의존성이 없습니다. frontend 폴더에서 npm.cmd install을 먼저 실행하세요."
}

Push-Location $frontendRoot
try {
    switch ($Action) {
        "Dev" { & npm.cmd run dev }
        "Build" { & npm.cmd run build }
        "Typecheck" { & npm.cmd run typecheck }
        "Test" { & npm.cmd run test }
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend $Action 실행이 종료 코드 $LASTEXITCODE 로 실패했습니다."
    }
}
finally {
    Pop-Location
}
