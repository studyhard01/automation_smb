[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $repoRoot
try {
    git config core.hooksPath .githooks
    $configured = git config --get core.hooksPath
    if ($configured -ne ".githooks") {
        throw "core.hooksPath 설정을 확인하지 못했습니다."
    }
    Write-Host "품질 pre-commit hook 활성화 완료: core.hooksPath=$configured"
    Write-Host "커밋 전에 clone별 .tmp/uv-cache로 개발 교훈 검토와 프로젝트 품질 평가가 실행됩니다."
}
finally {
    Pop-Location
}
