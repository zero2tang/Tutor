param([switch]$NoBrowser, [switch]$BackendOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$runtimeDir = Join-Path $projectRoot 'data\runtime'
$pythonPath = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
$vitePath = Join-Path $projectRoot 'frontend\node_modules\vite\bin\vite.js'
if (!(Test-Path -LiteralPath $pythonPath) -or !(Test-Path -LiteralPath $vitePath)) {
    throw 'Install backend and frontend dependencies first. See README.md.'
}
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$nodePath = (Get-Command node.exe -ErrorAction Stop).Source
$pidFile = Join-Path $runtimeDir 'processes.json'
$owned = @()
if (Test-Path -LiteralPath $pidFile) {
    $owned = @(Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json)
}

function Test-Backend {
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:8008/api/health' -TimeoutSec 5
        return $health.application -eq 'stem-tutor'
    } catch { return $false }
}

function Test-Frontend {
    try {
        $page = Invoke-WebRequest 'http://127.0.0.1:5173' -UseBasicParsing -TimeoutSec 3
        return $page.Content -match 'stem-tutor-web'
    } catch { return $false }
}

function Test-Reranker($url) {
    try {
        $health = Invoke-RestMethod "$url/health" -TimeoutSec 1
        return $health.service -eq 'tutor-qwen-reranker' -and $health.status -eq 'ready'
    } catch { return $false }
}

function Register-Process($process, $kind, $executable) {
    # 只记录本脚本创建的进程，停止时还要复核启动时间和绝对路径。
    $script:owned += [pscustomobject]@{
        id = $process.Id; kind = $kind
        started = $process.StartTime.ToUniversalTime().ToString('o')
        executable = $executable
    }
    ConvertTo-Json -InputObject @($script:owned) -Depth 4 | Set-Content -LiteralPath $pidFile -Encoding UTF8
}

$rerankerProcess = $null
Push-Location (Join-Path $projectRoot 'backend')
try {
    $rerankerUrl = & $pythonPath -B -c "from app.infrastructure.config import get_settings; s = get_settings(); print(s.exercise_rerank_url if s.exercise_rerank_enabled else '')"
    if ($LASTEXITCODE -ne 0) { throw 'Could not read backend settings.' }
} finally { Pop-Location }
if ($rerankerUrl) {
    $rerankerPython = Join-Path $runtimeDir 'qwen_rerank_env\Scripts\python.exe'
    if ($rerankerUrl -notmatch '^http://127\.0\.0\.1:([0-9]{1,5})$' -or [int]$Matches[1] -lt 1 -or [int]$Matches[1] -gt 65535) {
        Write-Warning 'Qwen reranker requires a loopback URL with an explicit port. Retrieval can still use RRF.'
    } elseif (!(Test-Reranker $rerankerUrl)) {
        $rerankerPort = ([uri]$rerankerUrl).Port
        if (Test-Path -LiteralPath $rerankerPython) {
            $rerankerProcess = Start-Process -FilePath $rerankerPython -ArgumentList @('-B','-m','local_reranker.server','--port',"$rerankerPort") -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'reranker.out.log') -RedirectStandardError (Join-Path $runtimeDir 'reranker.err.log') -PassThru
            Register-Process $rerankerProcess 'reranker' $rerankerPython
        } else { Write-Warning 'Qwen runtime is missing. Retrieval will use RRF if no local reranker is running.' }
    }
}

if (!(Test-Backend)) {
    $backendProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8008','--loop','app.infrastructure.persistence.async_runtime:selector_loop','--no-access-log') -WorkingDirectory (Join-Path $projectRoot 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'backend.out.log') -RedirectStandardError (Join-Path $runtimeDir 'backend.err.log') -PassThru
    Register-Process $backendProcess 'backend' $pythonPath
    for ($attempt = 0; $attempt -lt 25; $attempt++) {
        if (Test-Backend) { break }
        $backendProcess.Refresh()
        if ($backendProcess.HasExited) { throw "Backend exited. See $runtimeDir\backend.err.log" }
        Start-Sleep -Milliseconds 500
    }
    if (!(Test-Backend)) { throw "Backend startup timed out. See $runtimeDir\backend.err.log" }
}
if (!$BackendOnly -and !(Test-Frontend)) {
    $frontendProcess = Start-Process -FilePath $nodePath -ArgumentList @(('"{0}"' -f $vitePath),'--host','127.0.0.1','--port','5173','--strictPort') -WorkingDirectory (Join-Path $projectRoot 'frontend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'frontend.out.log') -RedirectStandardError (Join-Path $runtimeDir 'frontend.err.log') -PassThru
    Register-Process $frontendProcess 'frontend' $nodePath
    for ($attempt = 0; $attempt -lt 25; $attempt++) {
        if (Test-Frontend) { break }
        $frontendProcess.Refresh()
        if ($frontendProcess.HasExited) { throw "Frontend exited. See $runtimeDir\frontend.err.log" }
        Start-Sleep -Milliseconds 400
    }
    if (!(Test-Frontend)) { throw "Frontend startup timed out. See $runtimeDir\frontend.err.log" }
}
if ($rerankerProcess) {
    $deadline = [datetime]::UtcNow.AddSeconds(40)
    $ready = $false
    do {
        $rerankerProcess.Refresh()
        if ($rerankerProcess.HasExited) { break }
        $ready = Test-Reranker $rerankerUrl
        if (!$ready) { Start-Sleep -Milliseconds 500 }
    } until ($ready -or [datetime]::UtcNow -ge $deadline)
    if (!$ready) { Write-Warning "Qwen reranker is not ready. Retrieval can still use RRF. See $runtimeDir\reranker.err.log" }
}
Write-Host 'STEM Tutor: http://127.0.0.1:5173'
Write-Host 'API docs:   http://127.0.0.1:8008/docs'
Write-Host "Logs:       $runtimeDir"
if (!$NoBrowser) { Start-Process 'http://127.0.0.1:5173' }
