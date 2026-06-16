#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$OutputDir = "dist",
    [string]$PackageName = "",
    [switch]$IncludeGit
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$projectName = Split-Path -Leaf $projectRoot
$projectParent = Split-Path -Parent $projectRoot

if ([string]::IsNullOrWhiteSpace($PackageName)) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $PackageName = "aotu-gpt-tencent-$timestamp.tar.gz"
}

$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir
} else {
    Join-Path $projectRoot $OutputDir
}

New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$outputPath = Join-Path $outputRoot $PackageName

$tarCommand = Get-Command tar -ErrorAction SilentlyContinue
if (-not $tarCommand) {
    throw "tar command is required. Please run this script in PowerShell 7 with system tar available."
}

$excludePatterns = @(
    ".venv",
    "env",
    "venv",
    "node_modules",
    "cherry-studio-main",
    "cherry-studio-main.*",
    "data",
    "logs",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".uv-cache",
    ".codex-screenshots",
    ".idea",
    ".run",
    ".learnings",
    ".env",
    ".envrc",
    "*.log",
    "*.pid",
    "*.lock",
    "*.tmp",
    "*.temp",
    "*.bak",
    "*.db",
    "*.db-journal",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite",
    "*.sqlite3",
    "*.dump",
    "tmp_*.html",
    "tmp_*.png",
    "tmp_*.log"
)

if (-not $IncludeGit) {
    $excludePatterns += ".git"
}

$tarArgs = @("-czf", $outputPath)
foreach ($pattern in $excludePatterns) {
    $tarArgs += "--exclude=$projectName/$pattern"
}
$tarArgs += @("-C", $projectParent, $projectName)

Push-Location $projectRoot
try {
    & tar @tarArgs
    if ($LASTEXITCODE -ne 0) {
        throw "tar exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $outputPath
$hashPath = "$outputPath.sha256"
Set-Content -LiteralPath $hashPath -Value "$($hash.Hash.ToLowerInvariant())  $(Split-Path -Leaf $outputPath)" -Encoding UTF8

Write-Host "Tencent deployment package created:"
Write-Host "  $outputPath"
Write-Host "SHA256:"
Write-Host "  $hashPath"
