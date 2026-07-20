[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [ValidatePattern('^[^"]+$')]
    [string]$SessionName,

    [ValidateRange(1024, 65535)]
    [int]$LocalPort = 18080,

    [ValidateRange(1024, 65535)]
    [int]$RemotePort = 18080,

    [switch]$Foreground
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$logRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ".cache\local-stack\tunnels"))

function Assert-WorkspacePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $repoRoot.TrimEnd("\") + "\"
    if (-not $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "실행 경로가 저장소 밖입니다: $fullPath"
    }
    return $fullPath
}

function Test-LocalPort {
    param([Parameter(Mandatory = $true)][int]$TargetPort)

    $client = [System.Net.Sockets.TcpClient]::new()
    $asyncResult = $null
    try {
        $asyncResult = $client.BeginConnect("127.0.0.1", $TargetPort, $null, $null)
        if (-not $asyncResult.AsyncWaitHandle.WaitOne(250, $false)) {
            return $false
        }
        $client.EndConnect($asyncResult)
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $asyncResult) {
            $asyncResult.AsyncWaitHandle.Close()
        }
        $client.Close()
    }
}

$plinkCandidates = @(
    (Get-Command plink.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
    "C:\Program Files\PuTTY\plink.exe",
    "C:\Program Files (x86)\PuTTY\plink.exe",
    (Join-Path $env:LOCALAPPDATA "Programs\PuTTY\plink.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -Unique
$plink = $plinkCandidates | Select-Object -First 1
if (-not $plink) {
    throw "plink.exe를 찾지 못했습니다. PuTTY를 설치한 뒤 다시 실행하세요."
}
if (Test-LocalPort -TargetPort $LocalPort) {
    throw "127.0.0.1:$LocalPort 포트를 이미 다른 프로세스가 사용하고 있습니다."
}

$null = Assert-WorkspacePath $logRoot
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$stdoutLog = Assert-WorkspacePath (Join-Path $logRoot "embedding-tunnel-$LocalPort.stdout.log")
$stderrLog = Assert-WorkspacePath (Join-Path $logRoot "embedding-tunnel-$LocalPort.stderr.log")
$forward = "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}"
$arguments = @(
    "-load", "`"$SessionName`"",
    "-N",
    "-L", $forward
)

if ($Foreground) {
    & $plink @arguments
    exit $LASTEXITCODE
}

$backgroundArguments = @("-batch") + $arguments
$process = Start-Process `
    -FilePath $plink `
    -ArgumentList $backgroundArguments `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 250
    $process.Refresh()
    if ($process.HasExited) {
        throw "SSH 터널이 시작 중 종료됐습니다. PuTTY 세션과 로그를 확인하세요: $stderrLog"
    }
    if (Test-LocalPort -TargetPort $LocalPort) {
        $ready = $true
        break
    }
}
if (-not $ready) {
    Stop-Process -Id $process.Id
    throw "10초 안에 SSH 터널이 준비되지 않았습니다. 로그를 확인하세요: $stderrLog"
}

$endpoint = "http://127.0.0.1:$LocalPort/v1"
Write-Host "SSH 터널이 시작됐습니다."
Write-Host "PID: $($process.Id)"
Write-Host "Embedding endpoint: $endpoint"
Write-Host "종료: Stop-Process -Id $($process.Id)"

try {
    Invoke-WebRequest `
        -Uri "http://127.0.0.1:$LocalPort/health" `
        -Method Get `
        -TimeoutSec 2 `
        -UseBasicParsing | Out-Null
    Write-Host "원격 embedding 서버 health 확인: 정상"
}
catch {
    Write-Warning "터널은 열렸지만 원격 embedding 서버 health를 확인하지 못했습니다. 원격 서비스 상태를 점검하세요."
}
