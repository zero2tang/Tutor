param([ValidateSet('all', 'backend', 'frontend', 'reranker')][string]$Kind = 'all')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$pidFile = Join-Path $projectRoot 'data\runtime\processes.json'
if (!(Test-Path -LiteralPath $pidFile)) { Write-Host 'No processes owned by start.ps1.'; exit }
$records = @(Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json)
foreach ($record in $records) {
    if ($Kind -ne 'all' -and $record.kind -ne $Kind) { continue }
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if (!$process) { continue }
    $sameTime = $process.StartTime.ToUniversalTime().Ticks -eq ([datetime]$record.started).ToUniversalTime().Ticks
    $samePath = $process.Path -eq $record.executable
    $command = [string](Get-CimInstance Win32_Process -Filter "ProcessId=$($record.id)").CommandLine
    $pattern = switch ($record.kind) {
        'backend' { '-m uvicorn app\.main:app --host 127\.0\.0\.1 --port 8008\b' }
        'reranker' { '-m local_reranker\.server --port \d+\b' }
    }
    $expected = if ($pattern) {
        $expectedPath = if ($record.kind -eq 'backend') { 'backend\.venv\Scripts\python.exe' } else { 'data\runtime\qwen_rerank_env\Scripts\python.exe' }
        (!$record.executable -or $samePath) -and $process.Path -eq (Join-Path $projectRoot $expectedPath) -and $command -match $pattern
    } else {
        $samePath -and $command.Contains((Join-Path $projectRoot 'frontend\node_modules\vite\bin\vite.js'))
    }
    # PID 可能被系统复用，任何身份条件不匹配都不终止进程。
    if ($sameTime -and $expected) {
        if ($pattern) {
            # Windows venv 会派生实际解释器，只停止已核对父进程的服务子进程。
            foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($record.id)")) {
                if ($child.CommandLine -match $pattern -and $child.CreationDate.ToUniversalTime() -ge $process.StartTime.ToUniversalTime()) {
                    Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
                }
            }
        }
        Stop-Process -Id $record.id -ErrorAction SilentlyContinue
        Write-Host "Stopped $($record.kind) process $($record.id)."
    } else { Write-Host "Skipped changed process $($record.id)." }
}
