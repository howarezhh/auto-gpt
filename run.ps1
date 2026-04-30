$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $ProjectRoot ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$ActivateScript = Join-Path $VenvDir "Scripts\Activate.ps1"
$HostAddress = "127.0.0.1"
$Port = 8000
$PipIndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple"
$EnableReload = $false
$AllowForceKillPortProcess = $false

function Resolve-BootstrapPythonExe {
    $candidates = @(
        (Get-Command py -ErrorAction SilentlyContinue),
        (Get-Command python -ErrorAction SilentlyContinue),
        (Get-Command python3 -ErrorAction SilentlyContinue)
    ) | Where-Object { $null -ne $_ }

    foreach ($candidate in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate.Source)) {
            return $candidate.Source
        }
    }

    throw "未找到可用于创建项目虚拟环境的 Python。请先安装 Python 后重试。"
}

function Test-ProjectVenvPython {
    param(
        [string]$ProjectPythonExe,
        [string]$ProjectVenvDir
    )

    if (-not (Test-Path -LiteralPath $ProjectPythonExe)) {
        return $false
    }

    $expectedPrefix = (Resolve-Path -LiteralPath $ProjectVenvDir).Path
    $previousExpectedVenv = [Environment]::GetEnvironmentVariable("AOTU_GPT_EXPECTED_VENV")

    try {
        [Environment]::SetEnvironmentVariable("AOTU_GPT_EXPECTED_VENV", $expectedPrefix)
        & $ProjectPythonExe -c @"
import os
import sys

expected = os.path.normcase(os.path.abspath(os.environ["AOTU_GPT_EXPECTED_VENV"]))
actual_prefix = os.path.normcase(os.path.abspath(sys.prefix))
base_prefix = os.path.normcase(os.path.abspath(sys.base_prefix))
has_venv_cfg = os.path.isfile(os.path.join(sys.prefix, "pyvenv.cfg"))

sys.exit(0 if actual_prefix == expected and actual_prefix != base_prefix and has_venv_cfg else 1)
"@
        return $LASTEXITCODE -eq 0
    }
    finally {
        [Environment]::SetEnvironmentVariable("AOTU_GPT_EXPECTED_VENV", $previousExpectedVenv)
    }
}

function Get-DotEnvValues {
    param(
        [string]$EnvFilePath
    )

    $result = @{}
    if (-not (Test-Path $EnvFilePath)) {
        return $result
    }

    foreach ($rawLine in Get-Content -Path $EnvFilePath) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) {
            continue
        }
        $separatorIndex = $line.IndexOf("=")
        if ($separatorIndex -lt 1) {
            continue
        }
        $key = $line.Substring(0, $separatorIndex).Trim()
        $value = $line.Substring($separatorIndex + 1).Trim()
        $result[$key] = $value
    }

    return $result
}

function Get-EffectiveEnvValue {
    param(
        [string]$Name,
        [hashtable]$DotEnvValues,
        [string]$DefaultValue = ""
    )

    $processValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        return $processValue
    }
    if ($DotEnvValues.ContainsKey($Name) -and -not [string]::IsNullOrWhiteSpace($DotEnvValues[$Name])) {
        return $DotEnvValues[$Name]
    }
    return $DefaultValue
}

function Get-UrlEndpoint {
    param(
        [string]$Url
    )

    if ([string]::IsNullOrWhiteSpace($Url)) {
        return $null
    }

    $match = [regex]::Match(
        $Url,
        '^[a-zA-Z0-9+.-]+:\/\/(?:[^@\/?#]+@)?(?<host>[^:\/?#]+)(?::(?<port>\d+))?'
    )
    if (-not $match.Success) {
        return $null
    }

    $port = if ($match.Groups["port"].Success) { [int]$match.Groups["port"].Value } else { 0 }
    return @{
        Host = $match.Groups["host"].Value
        Port = $port
    }
}

function Test-TcpEndpoint {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutMs = 1200
    )

    if ([string]::IsNullOrWhiteSpace($HostName) -or $Port -le 0) {
        return $false
    }

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $asyncResult = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $asyncResult.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
            return $false
        }
        $client.EndConnect($asyncResult) | Out-Null
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Set-LocalDevRuntimeFallbacks {
    param(
        [string]$ProjectRootPath
    )

    $envFilePath = Join-Path $ProjectRootPath ".env"
    $dotEnvValues = Get-DotEnvValues -EnvFilePath $envFilePath
    $appEnv = (Get-EffectiveEnvValue -Name "APP_ENV" -DotEnvValues $dotEnvValues -DefaultValue "dev").Trim().ToLowerInvariant()

    if ($appEnv -in @("prod", "production")) {
        return
    }

    $databaseUrl = Get-EffectiveEnvValue -Name "DATABASE_URL" -DotEnvValues $dotEnvValues
    $databaseEndpoint = Get-UrlEndpoint -Url $databaseUrl
    if (
        -not [string]::IsNullOrWhiteSpace($databaseUrl) -and
        $databaseUrl.Trim().ToLowerInvariant().StartsWith("postgresql") -and
        $null -ne $databaseEndpoint -and
        $databaseEndpoint.Host -in @("127.0.0.1", "localhost") -and
        -not (Test-TcpEndpoint -HostName $databaseEndpoint.Host -Port $databaseEndpoint.Port)
    ) {
        $env:DATABASE_URL = "sqlite:///./data/app.db"
        Write-Warning "检测到本地 PostgreSQL 不可用，当前进程临时回退为 SQLite：$env:DATABASE_URL"
    }

    $redisUrl = Get-EffectiveEnvValue -Name "REDIS_URL" -DotEnvValues $dotEnvValues
    $redisEndpoint = Get-UrlEndpoint -Url $redisUrl
    if (
        -not [string]::IsNullOrWhiteSpace($redisUrl) -and
        $redisUrl.Trim().ToLowerInvariant().StartsWith("redis://") -and
        $null -ne $redisEndpoint -and
        $redisEndpoint.Host -in @("127.0.0.1", "localhost") -and
        -not (Test-TcpEndpoint -HostName $redisEndpoint.Host -Port $redisEndpoint.Port)
    ) {
        $env:REDIS_URL = ""
        Write-Warning "检测到本地 Redis 不可用，当前进程临时禁用 Redis 依赖；管理端可启动，但实时并发/限流相关能力不可用。"
    }
}

function Stop-ProjectProcesses {
    param(
        [string]$ProjectRootPath,
        [string]$ProjectPythonExe,
        [int]$ProjectPort
    )

    Write-Host "检查并停止该项目之前启动的进程..."

    $matchedProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        $commandLine = $_.CommandLine
        if ([string]::IsNullOrWhiteSpace($commandLine)) {
            return $false
        }

        $usesProjectPython = $commandLine -like "*$ProjectPythonExe*"
        $usesProjectRoot = $commandLine -like "*$ProjectRootPath*"
        $isProjectUvicorn = $commandLine -like "*-m uvicorn*" -and $commandLine -like "*app.main:app*"
        $matchesPort = $commandLine -like "*--port $ProjectPort*"

        return ($isProjectUvicorn -and ($usesProjectPython -or $usesProjectRoot -or $matchesPort))
    })

    if (-not $matchedProcesses -or $matchedProcesses.Count -eq 0) {
        Write-Host "未发现需要清理的旧进程。"
        return
    }

    $processIds = $matchedProcesses | Select-Object -ExpandProperty ProcessId -Unique
    foreach ($processId in $processIds) {
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Write-Host "已停止旧进程 PID: $processId"
        }
        catch {
            Write-Warning "停止旧进程 PID $processId 失败: $($_.Exception.Message)"
        }
    }

    Start-Sleep -Seconds 1
}

function Get-PortOwningProcessIds {
    param(
        [int]$Port
    )

    $pids = New-Object System.Collections.Generic.List[int]

    try {
        $connections = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction Stop)
        foreach ($connection in $connections) {
            if ($connection.OwningProcess -gt 0) {
                $pids.Add([int]$connection.OwningProcess)
            }
        }
    }
    catch {
    }

    try {
        $netstatLines = @(netstat -ano -p tcp | Select-String -Pattern "^\s*TCP\s+.+:$Port\s+")
        foreach ($line in $netstatLines) {
            $parts = ($line.Line -split "\s+") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
            if ($parts.Count -ge 5) {
                $pidCandidate = 0
                if ([int]::TryParse($parts[-1], [ref]$pidCandidate) -and $pidCandidate -gt 0) {
                    $pids.Add($pidCandidate)
                }
            }
        }
    }
    catch {
    }

    return @($pids | Sort-Object -Unique)
}

function Test-PortListening {
    param(
        [int]$Port
    )

    try {
        $listeningConnections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
        return $listeningConnections.Count -gt 0
    }
    catch {
        return $false
    }
}

function Stop-ProcessUsingPort {
    param(
        [int]$TargetPort,
        [bool]$ForceKill = $false
    )

    Write-Host "检查端口 $TargetPort 是否被占用..."

    if (-not (Test-PortListening -Port $TargetPort)) {
        Write-Host "端口 $TargetPort 当前未被占用。"
        return $true
    }

    if (-not $ForceKill) {
        $portPids = @(Get-PortOwningProcessIds -Port $TargetPort)
        Write-Warning "端口 $TargetPort 已被其他程序占用，出于安全考虑不会强制结束其它应用。占用 PID: $($portPids -join ', ')"
        return $false
    }

    $maxAttempts = 5
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        $portPids = @(Get-PortOwningProcessIds -Port $TargetPort)

        if (-not $portPids -or $portPids.Count -eq 0) {
            Write-Host "端口 $TargetPort 当前未被占用。"
            return
        }

        Write-Host "第 $attempt 次清理端口 $TargetPort，占用 PID: $($portPids -join ', ')"

        foreach ($processId in $portPids) {
            try {
                $process = Get-Process -Id $processId -ErrorAction Stop
                Write-Host "发现端口 ${TargetPort} 被进程占用: PID=$processId Name=$($process.ProcessName)"
            }
            catch {
                Write-Host "发现端口 ${TargetPort} 被进程占用: PID=$processId Name=<unknown>"
            }

            try {
                Stop-Process -Id $processId -Force -ErrorAction Stop
                Write-Host "已停止占用端口 $TargetPort 的进程 PID: $processId"
            }
            catch {
                try {
                    taskkill /PID $processId /F | Out-Null
                    Write-Host "已通过 taskkill 停止占用端口 $TargetPort 的进程 PID: $processId"
                }
                catch {
                    Write-Warning "停止占用端口 $TargetPort 的进程 PID $processId 失败: $($_.Exception.Message)"
                }
            }
        }

        Start-Sleep -Seconds 1
    }

    Start-Sleep -Seconds 2

    if (Test-PortListening -Port $TargetPort) {
        $remainingPids = @(Get-PortOwningProcessIds -Port $TargetPort)
        Write-Warning "端口 $TargetPort 仍处于监听状态，残留 PID: $($remainingPids -join ', ')"
        return $false
    }

    Write-Host "端口 $TargetPort 已释放。"
    return $true
}

function Resolve-AvailablePort {
    param(
        [int]$PreferredPort,
        [int]$MaxPort = 8010,
        [bool]$ForceKillPreferredPort = $false
    )

    $released = Stop-ProcessUsingPort -TargetPort $PreferredPort -ForceKill $ForceKillPreferredPort
    if ($released -and -not (Test-PortListening -Port $PreferredPort)) {
        return $PreferredPort
    }

    Write-Warning "端口 $PreferredPort 不可用，将尝试寻找可用端口。"

    for ($candidatePort = $PreferredPort + 1; $candidatePort -le $MaxPort; $candidatePort++) {
        if (-not (Test-PortListening -Port $candidatePort)) {
            Write-Host "已选择备用端口: $candidatePort"
            return $candidatePort
        }
    }

    throw "未找到可用端口。尝试范围: $PreferredPort-$MaxPort"
}

Set-Location $ProjectRoot
Set-LocalDevRuntimeFallbacks -ProjectRootPath $ProjectRoot

if (-not (Test-Path $VenvDir)) {
    Write-Host "未检测到项目虚拟环境，正在创建: $VenvDir"
    $BootstrapPythonExe = Resolve-BootstrapPythonExe
    & $BootstrapPythonExe -m venv $VenvDir
}

if (-not (Test-Path $PythonExe)) {
    throw "项目虚拟环境解释器不存在: $PythonExe"
}

if (-not (Test-ProjectVenvPython -ProjectPythonExe $PythonExe -ProjectVenvDir $VenvDir)) {
    throw "启动脚本拒绝继续：当前 Python 解释器未正确指向项目虚拟环境。期望: $PythonExe"
}

Stop-ProjectProcesses -ProjectRootPath $ProjectRoot -ProjectPythonExe $PythonExe -ProjectPort $Port
$Port = Resolve-AvailablePort -PreferredPort $Port -ForceKillPreferredPort $AllowForceKillPortProcess

if (Test-Path $ActivateScript) {
    . $ActivateScript
} else {
    throw "项目虚拟环境激活脚本不存在: $ActivateScript"
}

Write-Host "当前启动环境: 项目虚拟环境"
Write-Host "虚拟环境目录: $VenvDir"
Write-Host "虚拟环境激活脚本: $ActivateScript"
Write-Host "Python 解释器: $PythonExe"
Write-Host "服务地址: http://$HostAddress`:$Port"
Write-Host "启动模式: $(if ($EnableReload) { 'reload' } else { 'stable(no-reload)' })"

& $PythonExe -m pip install --upgrade pip -i $PipIndexUrl
& $PythonExe -m pip install -r requirements.txt -i $PipIndexUrl

$UvicornArgs = @("-m", "uvicorn", "app.main:app", "--host", $HostAddress, "--port", $Port)
if ($EnableReload) {
    $UvicornArgs += "--reload"
}

& $PythonExe @UvicornArgs
