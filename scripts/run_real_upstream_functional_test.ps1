param(
    [int]$ProxyPort = 8010,
    [string]$UpstreamBaseUrl = "https://aijh.huanmin.top/v1",
    [string]$ModelName = "",
    [int]$MaxConcurrency = 20,
    [int]$TotalRequests = 20,
    [double]$ClientTimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pwsh = "C:\Program Files\PowerShell\7\pwsh.exe"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$logDir = Join-Path $projectRoot "data\real-upstream-test-logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-PortListening {
    param([int]$Port)
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(500, $false)) {
            $client.Close()
            return $false
        }
        $client.EndConnect($async)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Stop-ListenersOnPort {
    param([int]$Port)
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        try {
            Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
        } catch {
        }
    }
}

if (-not $env:REAL_UPSTREAM_API_KEY) {
    throw "REAL_UPSTREAM_API_KEY is required"
}

$redisProcess = $null
if (-not (Test-PortListening -Port 6379)) {
    $redisCommand = Get-Command redis-server -ErrorAction SilentlyContinue
    if ($null -ne $redisCommand) {
        $redisProcess = Start-Process -FilePath $redisCommand.Source `
            -ArgumentList @("--port", "6379", "--save", "", "--appendonly", "no") `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logDir "redis.out.log") `
            -RedirectStandardError (Join-Path $logDir "redis.err.log") `
            -PassThru
        for ($i = 0; $i -lt 30; $i++) {
            if (Test-PortListening -Port 6379) {
                break
            }
            Start-Sleep -Milliseconds 300
        }
    }
}

Stop-ListenersOnPort -Port $ProxyPort

$serverCommand = @"
`$env:PYTHONPATH='$projectRoot'
`$env:ENABLE_BACKGROUND_WORKERS='true'
`$env:ENABLE_STARTUP_DB_INIT='false'
`$env:ENABLE_SCHEDULER='false'
`$env:ASYNC_REQUEST_LOG_ENABLED='false'
`$env:TOKEN_FINALIZE_WORKER_COUNT='1'
`$env:TOKEN_FINALIZE_QUEUE_SIZE='1000'
`$env:PROVIDER_MAX_ACTIVE_REQUESTS='30'
`$env:PROVIDER_MAX_ACTIVE_STREAMS='30'
`$env:GLOBAL_MAX_ACTIVE_REQUESTS='30'
`$env:GLOBAL_MAX_ACTIVE_STREAMS='30'
`$env:API_KEY_MAX_ACTIVE_REQUESTS='30'
`$env:API_KEY_MAX_ACTIVE_STREAMS='30'
`$env:ACCOUNT_MAX_ACTIVE_REQUESTS='30'
`$env:ACCOUNT_MAX_ACTIVE_STREAMS='30'
`$env:UPSTREAM_JSON_CLIENT='aiohttp'
`$env:UPSTREAM_STREAM_CLIENT='aiohttp'
& '$python' -m uvicorn app.main:app --host 127.0.0.1 --port $ProxyPort --no-access-log --log-level info
"@

$serverProcess = Start-Process -FilePath $pwsh `
    -ArgumentList @("-NoLogo", "-NoProfile", "-Command", $serverCommand) `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "server.out.log") `
    -RedirectStandardError (Join-Path $logDir "server.err.log") `
    -PassThru

try {
    $ready = $false
    for ($i = 0; $i -lt 80; $i++) {
        try {
            $response = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/live" -f $ProxyPort) -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        throw "local proxy did not become ready"
    }

    $testArgs = @(
        "scripts\real_upstream_functional_test.py",
        "--proxy-base-url", ("http://127.0.0.1:{0}" -f $ProxyPort),
        "--upstream-base-url", $UpstreamBaseUrl,
        "--max-concurrency", ([Math]::Min($MaxConcurrency, 30)),
        "--total-requests", ([Math]::Min($TotalRequests, 30)),
        "--client-timeout-s", $ClientTimeoutSeconds
    )
    if ($ModelName.Trim()) {
        $testArgs += @("--model-name", $ModelName.Trim())
    }

    & $python @testArgs
    if ($LASTEXITCODE -ne 0) {
        throw "functional test failed with exit code $LASTEXITCODE"
    }
} finally {
    if ($null -ne $serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Stop-ListenersOnPort -Port $ProxyPort
    if ($null -ne $redisProcess -and -not $redisProcess.HasExited) {
        Stop-Process -Id $redisProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
