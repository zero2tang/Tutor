param(
    [ValidateRange(8010,8099)][int]$Port = 8011,
    [ValidatePattern('^stem_tutor_[a-z0-9_]*acceptance$')][string]$Database = 'stem_tutor_memory_acceptance'
)
$ErrorActionPreference = 'Stop'
$acceptanceRoot = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))
$acceptanceRuntime = Join-Path $acceptanceRoot 'data\runtime'
$recordPath = Join-Path $acceptanceRuntime 'memory_context_process.json'
New-Item -ItemType Directory -Path $acceptanceRuntime -Force | Out-Null
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    if (!(Test-Path -LiteralPath $recordPath)) { throw '验收端口已占用，缺少本任务进程记录。' }
    $owned = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $server = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $launcher = Get-Process -Id $owned.id -ErrorAction SilentlyContinue
    $recordedStart = if ($owned.started -is [DateTime]) { $owned.started.ToUniversalTime() } else { [DateTimeOffset]::Parse([string]$owned.started).UtcDateTime }
    $portPattern = '--port\s+' + $Port + '(\s|$)'
    if (!$launcher -or $server.ParentProcessId -ne $owned.id -or $server.CommandLine -notmatch 'uvicorn app.main:app' -or $server.CommandLine -notmatch $portPattern -or [Math]::Abs(($launcher.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 3) {
        throw '验收进程归属校验失败，未停止任何服务。'
    }
    # 只重载本脚本创建的隔离验收服务，不触碰8008主学习服务。
    Stop-Process -Id $server.ProcessId
    if (Get-Process -Id $owned.id -ErrorAction SilentlyContinue) { Stop-Process -Id $owned.id }
}
$env:TUTOR_DATABASE = $Database
$env:TUTOR_CONTEXT_REQUEST_TOKENS = '32768'
$env:TUTOR_CONTEXT_WINDOW_TOKENS = '32768'
$env:TUTOR_CONTEXT_KEEP_RECENT_TURNS = '1'
$env:TUTOR_CONTEXT_SOFT_RATIO = '0.20'
$env:TUTOR_CONTEXT_TARGET_RATIO = '0.15'
$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $acceptanceRoot 'backend\.venv\Scripts\python.exe'
$process = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$Port",'--loop','app.infrastructure.persistence.async_runtime:selector_loop','--no-access-log') -WorkingDirectory (Join-Path $acceptanceRoot 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $acceptanceRuntime 'memory_context_backend.out.log') -RedirectStandardError (Join-Path $acceptanceRuntime 'memory_context_backend.err.log') -PassThru
[pscustomobject]@{
    id=$process.Id; port=$Port; database=$Database; executable=$process.Path
    started=$process.StartTime.ToUniversalTime().ToString('o')
    test_config=@{ keep_recent_turns=1; soft_ratio=0.20; target_ratio=0.15; request_tokens=32768 }
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $recordPath -Encoding UTF8
Write-Output "验收服务已启动 PID=$($process.Id) PORT=$Port DATABASE=$Database"
