param(
    [int]$MockPort = 18101,
    [int]$RedisPort = 6389,
    [int]$WebPortStart = 8050,
    [int]$WebWorkerCount = 14,
    [int]$RequestLogQueueWorkerCount = 1,
    [int]$RequestLogQueueBatchSize = 100,
    [int]$TokenFinalizeWorkerCount = 1,
    [switch]$RunBenchmark,
    [string]$BenchmarkEndpoint = "chat",
    [switch]$BenchmarkStream,
    [int]$WarmupRequests = 32,
    [int]$WarmupConcurrency = 8,
    [int]$BenchmarkRequests = 2000,
    [int]$BenchmarkConcurrency = 500,
    [string]$UpstreamJsonClient = "aiohttp",
    [string]$UpstreamStreamClient = "aiohttp",
    [switch]$SkipBackgroundWorker,
    [switch]$KeepRunning,
    [switch]$StopOnly
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pwsh = "C:\Program Files\PowerShell\7\pwsh.exe"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$logDir = Join-Path $projectRoot "data\bench-logs"
$webPorts = $WebPortStart..($WebPortStart + $WebWorkerCount - 1)
$allPorts = @($MockPort) + $webPorts
$benchmarkMutex = [System.Threading.Mutex]::new($false, "Local\aotu-gpt-benchmark-stack")
$benchmarkRedisUrl = "redis://127.0.0.1:$RedisPort/15"
$benchmarkApiKeyPrefix = "sk-aotu-benchmar"
$env:REDIS_URL = $benchmarkRedisUrl

function Test-PortListening {
    param([int]$Port)

    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(1000, $false)) {
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

function Stop-BenchmarkProcesses {
    param([int[]]$Ports)

    foreach ($port in $Ports) {
        $listeners = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        foreach ($listener in $listeners) {
            try {
                Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
            } catch {
            }
        }
    }
}

function New-ChildProcessCommand {
    param(
        [hashtable]$EnvMap,
        [string[]]$CommandLines
    )

    $lines = @()
    foreach ($item in $EnvMap.GetEnumerator()) {
        $lines += ('$env:{0}=''{1}''' -f $item.Key, $item.Value)
    }
    $lines += ('$env:PYTHONPATH=''{0}''' -f $projectRoot)
    $lines += $CommandLines
    return ($lines -join [Environment]::NewLine)
}

function Wait-PortsReady {
    param(
        [int]$RedisPort,
        [int]$MockPort,
        [int[]]$WebPorts,
        [int]$TimeoutSeconds = 20
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $redisReady = Test-PortListening -Port $RedisPort
        $mockReady = $false
        try {
            $mockResponse = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/v1/models" -f $MockPort) -UseBasicParsing -TimeoutSec 2
            $mockReady = $mockResponse.StatusCode -ge 200 -and $mockResponse.StatusCode -lt 500
        } catch {
        }
        $webReadyCount = 0
        foreach ($port in $WebPorts) {
            try {
                $webResponse = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/live" -f $port) -UseBasicParsing -TimeoutSec 2
                if ($webResponse.StatusCode -eq 200) {
                    $webReadyCount += 1
                }
            } catch {
            }
        }
        if ($redisReady -and $mockReady -and $webReadyCount -eq $WebPorts.Count) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
}

function Invoke-BenchmarkPython {
    param([string]$Code)

    $tempPath = [System.IO.Path]::ChangeExtension([System.IO.Path]::GetTempFileName(), ".py")
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $projectRoot
        Set-Content -LiteralPath $tempPath -Value $Code -Encoding UTF8
        & $python $tempPath
        if ($LASTEXITCODE -ne 0) {
            throw "Benchmark helper python exited with code $LASTEXITCODE"
        }
    } finally {
        $env:PYTHONPATH = $previousPythonPath
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}

function Reset-BenchmarkState {
    param(
        [string]$RedisUrl,
        [string]$ApiKeyPrefix
    )

    $code = @"
from sqlalchemy import delete
from app.database import SessionLocal
from app.models.request_log import RequestLog
from app.services.redis_service import RedisService

db = SessionLocal()
deleted = 0
try:
    result = db.execute(delete(RequestLog).where(RequestLog.api_client_key_prefix == "$ApiKeyPrefix"))
    deleted = result.rowcount or 0
    db.commit()
finally:
    db.close()

client = RedisService.get_sync_client()
client.flushdb()
print(f"benchmark_deleted_logs={deleted}")
"@
    Invoke-BenchmarkPython -Code $code
}

function Get-BenchmarkState {
    param(
        [string]$ApiKeyPrefix
    )

    $code = @"
import json
from sqlalchemy import func
from app.database import SessionLocal
from app.models.request_log import RequestLog
from app.services.redis_service import RedisService

client = RedisService.get_sync_client()
db = SessionLocal()
try:
    total_logs = db.query(func.count(RequestLog.id)).filter(RequestLog.api_client_key_prefix == "$ApiKeyPrefix").scalar() or 0
    pending_finalize = (
        db.query(func.count(RequestLog.id))
        .filter(
            RequestLog.api_client_key_prefix == "$ApiKeyPrefix",
            RequestLog.billing_finalized_at.is_(None),
        )
        .scalar()
        or 0
    )
finally:
    db.close()

print(json.dumps({
    "queued": int(client.llen("request_logs:queue") or 0),
    "processing": int(client.llen("request_logs:processing") or 0),
    "total_logs": int(total_logs),
    "pending_finalize": int(pending_finalize),
}))
"@
    $raw = Invoke-BenchmarkPython -Code $code
    return $raw | ConvertFrom-Json
}

function Wait-BenchmarkDrain {
    param(
        [string]$ApiKeyPrefix,
        [int]$ExpectedLogCount,
        [int]$TimeoutSeconds = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $stableCount = 0
    while ((Get-Date) -lt $deadline) {
        $state = Get-BenchmarkState -ApiKeyPrefix $ApiKeyPrefix
        Write-Host ("benchmark_queue_queued={0} processing={1} logs={2}/{3} pending_finalize={4}" -f `
            $state.queued, $state.processing, $state.total_logs, $ExpectedLogCount, $state.pending_finalize)
        if ([int]$state.queued -eq 0 -and [int]$state.processing -eq 0 -and [int]$state.pending_finalize -eq 0) {
            $stableCount += 1
            if ($stableCount -ge 2) {
                if ([int]$state.total_logs -lt $ExpectedLogCount) {
                    Write-Warning ("Benchmark reached idle state with {0} logged requests, lower than expected {1}. This usually means some client-side failures never reached the app." -f `
                        $state.total_logs, $ExpectedLogCount)
                }
                return
            }
        } else {
            $stableCount = 0
        }
        Start-Sleep -Seconds 1
    }

    throw "Benchmark background drain timed out"
}

function Ensure-RedisProcess {
    param(
        [int]$Port,
        [string]$LogDir
    )

    if (Test-PortListening -Port $Port) {
        return $null
    }

    $redisCommand = Get-Command redis-server -ErrorAction SilentlyContinue
    if ($null -eq $redisCommand) {
        throw "redis-server not found, but Redis is required for benchmark mode"
    }

    $process = Start-Process -FilePath $redisCommand.Source `
        -ArgumentList @(
            "--bind",
            "127.0.0.1",
            "--port",
            "$Port"
        ) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir "redis.out.log") `
        -RedirectStandardError (Join-Path $LogDir "redis.err.log")

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening -Port $Port) {
            return $process
        }
        Start-Sleep -Milliseconds 300
    }

    throw "redis-server did not start listening on port $Port"
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Stop-BenchmarkProcesses -Ports $allPorts

if ($StopOnly) {
    Write-Host "Stopped benchmark stack on ports: $($allPorts -join ', ')"
    exit 0
}

if (-not $benchmarkMutex.WaitOne(0)) {
    throw "Benchmark stack is already running in another process. Stop the existing run before starting a new one."
}

$redisProcess = Ensure-RedisProcess -Port $RedisPort -LogDir $logDir
Reset-BenchmarkState -RedisUrl $benchmarkRedisUrl -ApiKeyPrefix $benchmarkApiKeyPrefix

Start-Process -FilePath node `
    -ArgumentList @(
        "scripts\mock_openai_upstream_node.js",
        "--host",
        "127.0.0.1",
        "--port",
        "$MockPort",
        "--delay-ms",
        "0",
        "--stream-chunk-delay-ms",
        "0",
        "--stream-chunks",
        "4",
        "--output-chars",
        "128"
    ) `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "mock.out.log") `
    -RedirectStandardError (Join-Path $logDir "mock.err.log") | Out-Null

if (-not $SkipBackgroundWorker) {
    $workerCommand = New-ChildProcessCommand `
        -EnvMap @{
            REDIS_URL = $benchmarkRedisUrl
            ENABLE_BACKGROUND_WORKERS = "true"
            ENABLE_STARTUP_DB_INIT = "false"
            ENABLE_SCHEDULER = "false"
            REQUEST_LOG_QUEUE_WORKER_COUNT = "$RequestLogQueueWorkerCount"
            REQUEST_LOG_QUEUE_BATCH_SIZE = "$RequestLogQueueBatchSize"
            TOKEN_FINALIZE_WORKER_COUNT = "$TokenFinalizeWorkerCount"
            TOKEN_FINALIZE_QUEUE_SIZE = "10000"
            DB_POOL_SIZE = "6"
            DB_MAX_OVERFLOW = "4"
            DB_POOL_TIMEOUT = "10"
            UPSTREAM_JSON_CLIENT = $UpstreamJsonClient
            UPSTREAM_STREAM_CLIENT = $UpstreamStreamClient
        } `
        -CommandLines @(
            ('& ''{0}'' -m scripts.run_background_workers' -f $python)
        )

    Start-Process -FilePath $pwsh `
        -ArgumentList @("-NoLogo", "-NoProfile", "-Command", $workerCommand) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir "worker.out.log") `
        -RedirectStandardError (Join-Path $logDir "worker.err.log") | Out-Null
}

foreach ($port in $webPorts) {
    $webCommand = New-ChildProcessCommand `
        -EnvMap @{
            REDIS_URL = $benchmarkRedisUrl
            ENABLE_BACKGROUND_WORKERS = "false"
            ENABLE_STARTUP_DB_INIT = "false"
            ENABLE_SCHEDULER = "false"
            ASYNC_REQUEST_LOG_ENABLED = "true"
            REQUEST_LOG_QUEUE_BATCH_SIZE = "$RequestLogQueueBatchSize"
            REQUEST_LOG_INGRESS_QUEUE_SIZE = "20000"
            DB_POOL_SIZE = "3"
            DB_MAX_OVERFLOW = "2"
            DB_POOL_TIMEOUT = "10"
            WORKER_THREADPOOL_TOKENS = "100"
            UPSTREAM_JSON_CLIENT = $UpstreamJsonClient
            UPSTREAM_STREAM_CLIENT = $UpstreamStreamClient
            CACHE_L1_TTL_CAP_SECONDS = "1"
            API_KEY_AUTH_L1_CACHE_TTL_SECONDS = "1"
            API_KEY_AUTH_USAGE_INVALIDATE_INTERVAL_MS = "0"
            ROUTE_CAPACITY_PREFILTER_ENABLED = "false"
            GLOBAL_MAX_ACTIVE_REQUESTS = "2000"
            GLOBAL_MAX_ACTIVE_STREAMS = "2000"
            API_KEY_MAX_ACTIVE_REQUESTS = "2000"
            API_KEY_MAX_ACTIVE_STREAMS = "2000"
            ACCOUNT_MAX_ACTIVE_REQUESTS = "2000"
            ACCOUNT_MAX_ACTIVE_STREAMS = "2000"
            PROVIDER_MAX_ACTIVE_REQUESTS = "2000"
            PROVIDER_MAX_ACTIVE_STREAMS = "2000"
        } `
        -CommandLines @(
            ('& ''{0}'' -m uvicorn app.main:app --host 127.0.0.1 --port {1} --no-access-log --log-level warning' -f $python, $port)
        )

    Start-Process -FilePath $pwsh `
        -ArgumentList @("-NoLogo", "-NoProfile", "-Command", $webCommand) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir ("web-{0}.out.log" -f $port)) `
        -RedirectStandardError (Join-Path $logDir ("web-{0}.err.log" -f $port)) | Out-Null
}

Wait-PortsReady -RedisPort $RedisPort -MockPort $MockPort -WebPorts $webPorts

$results = foreach ($port in $allPorts) {
    $ready = $false
    if ($port -eq $MockPort) {
        try {
            $response = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/v1/models" -f $port) -UseBasicParsing -TimeoutSec 2
            $ready = $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
        } catch {
        }
    } else {
        try {
            $response = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/live" -f $port) -UseBasicParsing -TimeoutSec 2
            $ready = $response.StatusCode -eq 200
        } catch {
        }
    }
    [pscustomobject]@{
        Port = $port
        Listening = $ready
    }
}

$results | Format-Table -AutoSize

if (-not $RunBenchmark) {
    exit 0
}

$baseUrls = ($webPorts | ForEach-Object { "http://127.0.0.1:{0}" -f $_ }) -join ","
$warmupArgs = @(
    "scripts\benchmark_proxy.py",
    "--base-url",
    $baseUrls,
    "--mock-base-url",
    ("http://127.0.0.1:{0}/v1" -f $MockPort),
    "--requests",
    "$WarmupRequests",
    "--warmup",
    "0",
    "--concurrency",
    "$WarmupConcurrency",
    "--endpoint",
    $BenchmarkEndpoint
)
if ($BenchmarkStream) {
    $warmupArgs += "--stream"
}

$benchmarkArgs = @(
    "scripts\benchmark_proxy_node.js",
    "--base-url",
    $baseUrls,
    "--endpoint",
    $BenchmarkEndpoint,
    "--requests",
    "$BenchmarkRequests",
    "--concurrency",
    "$BenchmarkConcurrency"
)
if ($BenchmarkStream) {
    $benchmarkArgs += "--stream"
}

try {
    & $python @warmupArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Warmup benchmark failed with exit code $LASTEXITCODE"
    }
    & node @benchmarkArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Node benchmark failed with exit code $LASTEXITCODE"
    }
    if (-not $SkipBackgroundWorker) {
        Wait-BenchmarkDrain -ApiKeyPrefix $benchmarkApiKeyPrefix -ExpectedLogCount ($WarmupRequests + $BenchmarkRequests)
    }
} finally {
    try {
        Reset-BenchmarkState -RedisUrl $benchmarkRedisUrl -ApiKeyPrefix $benchmarkApiKeyPrefix
    } catch {
        Write-Warning $_
    }
    if (-not $KeepRunning) {
        Stop-BenchmarkProcesses -Ports $allPorts
        if ($null -ne $redisProcess) {
            try {
                Stop-Process -Id $redisProcess.Id -Force -ErrorAction SilentlyContinue
            } catch {
            }
        }
    }
}
