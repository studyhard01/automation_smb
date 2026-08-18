[CmdletBinding()]
param(
    [ValidateSet("Start", "Stop", "Status", "Plan")]
    [string]$Action = "Start",
    [int]$Port = 8011,
    [switch]$NoReload,
    [switch]$Visible
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$cacheRoot = Join-Path $repoRoot ".cache\local-stack"
$stateRoot = Join-Path $cacheRoot "state"
$logRoot = Join-Path $cacheRoot "logs"
$stateFile = Join-Path $stateRoot "playground.json"

function Test-LocalPort {
    param([Parameter(Mandatory = $true)][int]$TargetPort)
    $client = [System.Net.Sockets.TcpClient]::new()
    $result = $null
    try {
        $result = $client.BeginConnect("127.0.0.1", $TargetPort, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(250, $false)) { return $false }
        $client.EndConnect($result)
        return $true
    }
    catch { return $false }
    finally {
        if ($null -ne $result) { $result.AsyncWaitHandle.Close() }
        $client.Close()
    }
}

$reloadArgs = if ($NoReload) { "" } else { " --reload --reload-dir backend/src" }
$command = "uv run --no-sync uvicorn smb_finder.api:app --app-dir backend/src --host 127.0.0.1 --port $Port$reloadArgs"

if ($Action -eq "Plan") {
    Write-Host "WorkDir: $repoRoot"
    Write-Host "Command: $command"
    Write-Host "URL: http://127.0.0.1:$Port/playground"
    exit 0
}

if ($Action -eq "Status") {
    $served = Test-LocalPort -TargetPort $Port
    Write-Host "Playground port ${Port}: $(if ($served) { 'listening' } else { 'stopped' })"
    if (Test-Path -LiteralPath $stateFile) { Get-Content -LiteralPath $stateFile -Raw }
    exit 0
}

if ($Action -eq "Stop") {
    if (-not (Test-Path -LiteralPath $stateFile)) {
        Write-Host "관리 중인 Playground 프로세스가 없습니다."
        exit 0
    }
    $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $process = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
    if ($null -ne $process) { Stop-Process -Id $process.Id }
    Remove-Item -LiteralPath $stateFile -Force
    Write-Host "Playground를 중지했습니다."
    exit 0
}

if (Test-LocalPort -TargetPort $Port) {
    Write-Host "http://127.0.0.1:$Port/playground 에 이미 서비스가 실행 중입니다."
    exit 0
}

New-Item -ItemType Directory -Force -Path $stateRoot, $logRoot | Out-Null
$stdoutLog = Join-Path $logRoot "playground.stdout.log"
$stderrLog = Join-Path $logRoot "playground.stderr.log"
$windowStyle = if ($Visible) { "Normal" } else { "Hidden" }
$process = Start-Process -FilePath "powershell.exe" -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-Command", "Set-Location -LiteralPath '$repoRoot'; $command"
) -WorkingDirectory $repoRoot -WindowStyle $windowStyle -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru

[ordered]@{
    pid = $process.Id
    port = $Port
    started_at_utc = $process.StartTime.ToUniversalTime().ToString("o")
    stdout_log = $stdoutLog
    stderr_log = $stderrLog
} | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8

Write-Host "Playground 시작 요청 완료: http://127.0.0.1:$Port/playground"
