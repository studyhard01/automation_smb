[CmdletBinding()]
param(
    [ValidateSet("Start", "Stop", "Status", "Plan")]
    [string]$Action = "Start",

    [ValidateSet("Playground", "Mlflow", "Studio", "All")]
    [string]$Profile = "All",

    [switch]$NoReload,
    [switch]$Visible
)

$ErrorActionPreference = "Stop"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$cacheRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ".cache\local-stack"))
$stateRoot = Join-Path $cacheRoot "state"
$logRoot = Join-Path $cacheRoot "logs"
$uvCacheRoot = Join-Path $cacheRoot "uv-cache"
$mlflowRoot = Join-Path $cacheRoot "mlflow"
$mlflowArtifactRoot = Join-Path $mlflowRoot "artifacts"
$mlflowDbPath = Join-Path $mlflowRoot "mlflow.db"
$studioDir = Join-Path $repoRoot "integrations\langgraph"
$studioScript = Join-Path $studioDir "start_local_studio.ps1"

function Assert-WorkspacePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $repoRoot.TrimEnd("\") + "\"
    if (-not $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Local stack path is outside the repository: $fullPath"
    }
    return $fullPath
}

function ConvertTo-PowerShellLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

function Test-LocalPort {
    param([Parameter(Mandatory = $true)][int]$Port)

    $client = [System.Net.Sockets.TcpClient]::new()
    $asyncResult = $null
    try {
        $asyncResult = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
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

function Get-LocalPortProcessIds {
    param([Parameter(Mandatory = $true)][int]$Port)

    $netstat = Join-Path $env:SystemRoot "System32\netstat.exe"
    $pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    $processIds = @()
    foreach ($line in (& $netstat -ano -p TCP)) {
        $match = [regex]::Match([string]$line, $pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
        if ($match.Success) {
            $processIds += [int]$match.Groups[1].Value
        }
    }
    return @($processIds | Sort-Object -Unique)
}

function Normalize-ProcessPathEnvironment {
    # Codex/일부 Windows launcher는 PATH와 Path를 동시에 넣어 Start-Process가 중복 키 오류를 낼 수 있다.
    $pathValue = [Environment]::GetEnvironmentVariable("Path", "Process")
    if (-not [string]::IsNullOrWhiteSpace($pathValue)) {
        [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
        [Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
    }
}

function Get-StateFile {
    param([Parameter(Mandatory = $true)][string]$ServiceKey)

    return Assert-WorkspacePath (Join-Path $stateRoot "$ServiceKey.json")
}

function Read-ServiceState {
    param([Parameter(Mandatory = $true)][string]$ServiceKey)

    $stateFile = Get-StateFile $ServiceKey
    if (-not (Test-Path -LiteralPath $stateFile)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $stateFile -Encoding utf8 -Raw | ConvertFrom-Json
    }
    catch {
        throw "Cannot read local stack state file: $stateFile"
    }
}

function Remove-ServiceState {
    param([Parameter(Mandatory = $true)][string]$ServiceKey)

    $stateFile = Get-StateFile $ServiceKey
    if (Test-Path -LiteralPath $stateFile) {
        Remove-Item -LiteralPath $stateFile -Force
    }
}

function Get-ServiceProcessState {
    param([Parameter(Mandatory = $true)]$Service)

    $state = Read-ServiceState $Service.Key
    if ($null -eq $state) {
        return [pscustomobject]@{ Kind = "none"; State = $null; Process = $null }
    }

    $process = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return [pscustomobject]@{ Kind = "stale"; State = $state; Process = $null }
    }

    try {
        $expectedStart = [DateTimeOffset]::Parse([string]$state.started_at_utc).UtcDateTime
        $actualStart = $process.StartTime.ToUniversalTime()
        $startDeltaSeconds = [Math]::Abs(($actualStart - $expectedStart).TotalSeconds)
    }
    catch {
        return [pscustomobject]@{ Kind = "mismatch"; State = $state; Process = $process }
    }

    if ($process.ProcessName -notmatch "^powershell$" -or $startDeltaSeconds -gt 5) {
        return [pscustomobject]@{ Kind = "mismatch"; State = $state; Process = $process }
    }
    return [pscustomobject]@{ Kind = "owned"; State = $state; Process = $process }
}

function Write-ServiceState {
    param(
        [Parameter(Mandatory = $true)]$Service,
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [string]$StdoutLog = "",
        [string]$StderrLog = ""
    )

    $stateFile = Get-StateFile $Service.Key
    $state = [ordered]@{
        service = $Service.Key
        pid = $Process.Id
        started_at_utc = $Process.StartTime.ToUniversalTime().ToString("o")
        port = $Service.Port
        stdout_log = $StdoutLog
        stderr_log = $StderrLog
        repo_root = $repoRoot
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8
}

function New-WrapperCommand {
    param([Parameter(Mandatory = $true)]$Service)

    $marker = "automation_smb_local_stack:$($Service.Key)"
    $workDirLiteral = ConvertTo-PowerShellLiteral $Service.WorkDir
    $uvCacheLiteral = ConvertTo-PowerShellLiteral $uvCacheRoot
    return @"
`$ErrorActionPreference = 'Stop'
`$env:AUTOMATION_SMB_STACK_SERVICE = '$marker'
`$env:PYTHONUTF8 = '1'
`$env:PYTHONIOENCODING = 'utf-8'
`$env:UV_CACHE_DIR = $uvCacheLiteral
Set-Location -LiteralPath $workDirLiteral
$($Service.Command)
if (`$LASTEXITCODE -ne 0) { exit `$LASTEXITCODE }
"@
}

function Start-StackService {
    param([Parameter(Mandatory = $true)]$Service)

    $processState = Get-ServiceProcessState $Service
    if ($processState.Kind -eq "owned") {
        Write-Host "[$($Service.DisplayName)] already running (PID $($processState.Process.Id), port $($Service.Port))."
        return $false
    }
    if ($processState.Kind -eq "stale") {
        Remove-ServiceState $Service.Key
    }
    if ($processState.Kind -eq "mismatch") {
        throw "[$($Service.DisplayName)] state PID belongs to another process. Refusing to replace it: $(Get-StateFile $Service.Key)"
    }
    if (Test-LocalPort $Service.Port) {
        Write-Host "[$($Service.DisplayName)] port $($Service.Port) is already served by an external process; reusing it."
        return $false
    }

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv command was not found. Install uv or add it to PATH."
    }
    if ($Service.Key -eq "studio" -and -not (Test-Path -LiteralPath $studioScript)) {
        throw "LangGraph startup script is missing: $studioScript"
    }

    $wrapperCommand = New-WrapperCommand $Service
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapperCommand))
    $startArguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encodedCommand)
    $startParameters = @{
        FilePath = "powershell.exe"
        ArgumentList = $startArguments
        PassThru = $true
        WindowStyle = $(if ($Visible) { "Normal" } else { "Hidden" })
    }

    $stdoutLog = ""
    $stderrLog = ""
    if (-not $Visible) {
        $stdoutLog = Assert-WorkspacePath (Join-Path $logRoot "$($Service.Key).out.log")
        $stderrLog = Assert-WorkspacePath (Join-Path $logRoot "$($Service.Key).err.log")
        $startParameters["RedirectStandardOutput"] = $stdoutLog
        $startParameters["RedirectStandardError"] = $stderrLog
    }

    Normalize-ProcessPathEnvironment
    $process = Start-Process @startParameters
    Start-Sleep -Milliseconds 300
    $process.Refresh()
    if ($process.HasExited) {
        throw "[$($Service.DisplayName)] exited during startup. Check $stderrLog"
    }

    Write-ServiceState -Service $Service -Process $process -StdoutLog $stdoutLog -StderrLog $stderrLog
    Write-Host "[$($Service.DisplayName)] starting (PID $($process.Id), port $($Service.Port))."
    if (-not $Visible) {
        Write-Host "  logs: $stdoutLog / $stderrLog"
    }
    return $true
}

function Stop-StackService {
    param([Parameter(Mandatory = $true)]$Service)

    $processState = Get-ServiceProcessState $Service
    if ($processState.Kind -eq "none") {
        if (Test-LocalPort $Service.Port) {
            Write-Host "[$($Service.DisplayName)] external process is using port $($Service.Port); leaving it running."
        }
        else {
            Write-Host "[$($Service.DisplayName)] not running."
        }
        return
    }
    if ($processState.Kind -eq "stale") {
        Remove-ServiceState $Service.Key
        Write-Host "[$($Service.DisplayName)] removed stale state."
        return
    }
    if ($processState.Kind -eq "mismatch") {
        Write-Warning "[$($Service.DisplayName)] PID ownership mismatch; refusing to stop PID $($processState.State.pid)."
        return
    }

    $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $taskkillOutput = & $taskkill /PID $processState.Process.Id /T /F 2>&1
        $taskkillExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($taskkillExitCode -ne 0) {
        $earliestChildStart = $processState.Process.StartTime.AddSeconds(-5)
        foreach ($listenerPid in (Get-LocalPortProcessIds $Service.Port)) {
            $listenerProcess = Get-Process -Id $listenerPid -ErrorAction SilentlyContinue
            if ($null -eq $listenerProcess) {
                continue
            }
            if ($listenerProcess.StartTime -lt $earliestChildStart) {
                throw (
                    "[$($Service.DisplayName)] port $($Service.Port) belongs to an older process. " +
                    "Refusing to stop PID $listenerPid."
                )
            }
            Stop-Process -Id $listenerPid -Force -ErrorAction Stop
        }
        try {
            $wrapperProcess = Get-Process -Id $processState.Process.Id -ErrorAction SilentlyContinue
            if ($null -ne $wrapperProcess) {
                Stop-Process -Id $processState.Process.Id -Force -ErrorAction Stop
            }
            Start-Sleep -Milliseconds 500
        }
        catch {
            throw "[$($Service.DisplayName)] failed to stop PID $($processState.Process.Id): $taskkillOutput"
        }
        if (Test-LocalPort $Service.Port) {
            throw (
                "[$($Service.DisplayName)] wrapper stopped but port $($Service.Port) is still active. " +
                "Run taskkill for PID $($processState.Process.Id) from an authorized terminal."
            )
        }
        Write-Warning "[$($Service.DisplayName)] taskkill was unavailable; stopped the wrapper with Stop-Process."
    }
    Remove-ServiceState $Service.Key
    Write-Host "[$($Service.DisplayName)] stopped."
}

function Show-StackServiceStatus {
    param([Parameter(Mandatory = $true)]$Service)

    $processState = Get-ServiceProcessState $Service
    $portReady = Test-LocalPort $Service.Port
    switch ($processState.Kind) {
        "owned" {
            $readiness = if ($portReady) { "ready" } else { "starting" }
            Write-Host "[$($Service.DisplayName)] $readiness (PID $($processState.Process.Id), port $($Service.Port))."
        }
        "stale" {
            Remove-ServiceState $Service.Key
            Write-Host "[$($Service.DisplayName)] stopped (stale state removed)."
        }
        "mismatch" {
            Write-Warning "[$($Service.DisplayName)] state PID ownership mismatch."
        }
        default {
            if ($portReady) {
                Write-Host "[$($Service.DisplayName)] external process is ready on port $($Service.Port)."
            }
            else {
                Write-Host "[$($Service.DisplayName)] stopped."
            }
        }
    }
}

$studioScriptLiteral = ConvertTo-PowerShellLiteral $studioScript
$mlflowArtifactUri = [System.Uri]::new($mlflowArtifactRoot).AbsoluteUri
$mlflowArtifactLiteral = ConvertTo-PowerShellLiteral $mlflowArtifactUri
$mlflowDbUri = "sqlite:///" + ($mlflowDbPath -replace "\\", "/")
$mlflowDbUriLiteral = ConvertTo-PowerShellLiteral $mlflowDbUri
$reloadArgument = if ($NoReload) { "" } else { " --reload" }

$serviceCatalog = [ordered]@{
    playground = [pscustomobject]@{
        Key = "playground"
        DisplayName = "Playground"
        Port = 8010
        WorkDir = $repoRoot
        Command = "& uv run --native-tls uvicorn smb_finder.api:app --host 127.0.0.1 --port 8010$reloadArgument"
    }
    mlflow = [pscustomobject]@{
        Key = "mlflow"
        DisplayName = "MLflow"
        Port = 5000
        WorkDir = $repoRoot
        Command = (
            "& uv run --native-tls --with 'mlflow[genai]>=3.9,<4' mlflow server " +
            "--host 127.0.0.1 --port 5000 --backend-store-uri $mlflowDbUriLiteral " +
            "--artifacts-destination $mlflowArtifactLiteral"
        )
    }
    studio = [pscustomobject]@{
        Key = "studio"
        DisplayName = "LangGraph Studio"
        Port = 2024
        WorkDir = $studioDir
        Command = "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $studioScriptLiteral"
    }
}

switch ($Profile) {
    "Playground" { $selectedKeys = @("playground") }
    "Mlflow" { $selectedKeys = @("mlflow") }
    "Studio" { $selectedKeys = @("playground", "studio") }
    "All" { $selectedKeys = @("playground", "mlflow", "studio") }
}
$selectedServices = @($selectedKeys | ForEach-Object { $serviceCatalog[$_] })

if ($Action -eq "Plan") {
    Write-Host "Local stack plan: profile=$Profile"
    foreach ($service in $selectedServices) {
        Write-Host "[$($service.DisplayName)] port $($service.Port)"
        Write-Host "  workdir: $($service.WorkDir)"
        Write-Host "  command: $($service.Command)"
    }
    exit 0
}

if ($Action -eq "Status") {
    foreach ($service in $selectedServices) {
        Show-StackServiceStatus $service
    }
    exit 0
}

if ($Action -eq "Start") {
    New-Item -ItemType Directory -Force $stateRoot, $logRoot, $uvCacheRoot, $mlflowArtifactRoot | Out-Null
    foreach ($service in $selectedServices) {
        Start-StackService $service | Out-Null
    }
    Write-Host "Use '-Action Status -Profile $Profile' to check readiness."
    Write-Host "Use '-Action Stop -Profile $Profile' to stop processes started by this script."
    exit 0
}

$servicesToStop = @($selectedServices)
[Array]::Reverse($servicesToStop)
foreach ($service in $servicesToStop) {
    Stop-StackService $service
}
