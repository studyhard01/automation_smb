[CmdletBinding()]
param(
    [string]$BaseEnv = ".env",
    [string]$DockerEnv = ".env.docker"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$basePath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $BaseEnv))
$dockerPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $DockerEnv))
$runtimePath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ".runtime"))

foreach ($path in @($basePath, $dockerPath)) {
    if (-not $path.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Environment files must stay inside the repository."
    }
}
if (-not (Test-Path -LiteralPath $basePath)) {
    throw ".env is missing. Copy .env.example and configure it first."
}
if (-not (Test-Path -LiteralPath $runtimePath)) {
    New-Item -ItemType Directory -Path $runtimePath | Out-Null
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        $values[$parts[0].Trim()] = $parts[1].Trim().Trim('"').Trim("'")
    }
    return $values
}

$baseValues = Read-DotEnv $basePath
$dockerValues = @{}
if (Test-Path -LiteralPath $dockerPath) {
    $dockerValues = Read-DotEnv $dockerPath
}
if ($dockerValues.ContainsKey("LLMOPS_DB_HOST") -and $dockerValues["LLMOPS_DB_HOST"]) {
    Write-Output "Kept the existing PostgreSQL override in .env.docker."
    exit 0
}

$dbHost = $baseValues["LLMOPS_DB_HOST"]
if (-not $dbHost) {
    $dbHost = $baseValues["DEV_SERVER"]
}
if (-not $dbHost) {
    $dbHost = $baseValues["POSTGRES_HOST"]
}
if (-not $dbHost) {
    throw "No PostgreSQL host candidate is configured."
}

$lines = [System.Collections.Generic.List[string]]::new()
if (Test-Path -LiteralPath $dockerPath) {
    $lines.AddRange([System.IO.File]::ReadAllLines($dockerPath))
}
for ($index = $lines.Count - 1; $index -ge 0; $index--) {
    if ($lines[$index] -match '^\s*LLMOPS_DB_HOST\s*=') {
        $lines.RemoveAt($index)
    }
}
if ($lines.Count -gt 0 -and $lines[$lines.Count - 1]) {
    $lines.Add("")
}
$lines.Add("# Container-only endpoint generated without printing its value")
$lines.Add("LLMOPS_DB_HOST=$dbHost")
[System.IO.File]::WriteAllLines($dockerPath, $lines, [System.Text.UTF8Encoding]::new($false))

Write-Output "Prepared the PostgreSQL container endpoint override (value hidden)."
