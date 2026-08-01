param(
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "start",
    [int]$BackendPort = 8765,
    [int]$FrontendPort = 5173,
    [int]$Steps = 480,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime"
$BackendPidPath = Join-Path $RuntimeDir "backend.pid"
$FrontendPidPath = Join-Path $RuntimeDir "frontend.pid"
$BackendLogPath = Join-Path $RuntimeDir "backend.log"
$BackendErrorPath = Join-Path $RuntimeDir "backend.error.log"
$FrontendLogPath = Join-Path $RuntimeDir "frontend.log"
$FrontendErrorPath = Join-Path $RuntimeDir "frontend.error.log"

function Test-LocalPort([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $task.Wait(500)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Resolve-BackendPython {
    $candidates = @()
    if ($PythonPath) { $candidates += $PythonPath }
    $candidates += Join-Path $ProjectRoot ".venv\\Scripts\\python.exe"
    if ($env:CONDA_PREFIX) {
        $candidates += Join-Path $env:CONDA_PREFIX "python.exe"
    }
    $candidates += "C:\\InstallPack\\miniconda\\python.exe"
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($systemPython) { $candidates += $systemPython.Source }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        & $candidate -c "import uvicorn" 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    throw "No Python interpreter with uvicorn was found. Use -PythonPath to select the project environment."
}

function Get-ManagedProcess([string]$PidPath) {
    if (-not (Test-Path -LiteralPath $PidPath)) { return $null }
    $pidValue = [int](Get-Content -LiteralPath $PidPath -Raw)
    return Get-Process -Id $pidValue -ErrorAction SilentlyContinue
}

function Stop-ManagedProcess([string]$PidPath) {
    $process = Get-ManagedProcess $PidPath
    if ($null -ne $process) {
        Stop-Process -Id $process.Id -Force
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
}

function Wait-ForPort([int]$Port, [int]$TimeoutSeconds, [string]$Name) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-LocalPort $Port) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name did not listen on port $Port within $TimeoutSeconds seconds."
}

function Show-Status {
    $backend = Get-ManagedProcess $BackendPidPath
    $frontend = Get-ManagedProcess $FrontendPidPath
    [PSCustomObject]@{
        BackendProcess = if ($backend) { "$($backend.ProcessName) ($($backend.Id))" } else { "stopped" }
        BackendPort = Test-LocalPort $BackendPort
        FrontendProcess = if ($frontend) { "$($frontend.ProcessName) ($($frontend.Id))" } else { "stopped" }
        FrontendPort = Test-LocalPort $FrontendPort
        FrontendUrl = "http://127.0.0.1:$FrontendPort"
    } | Format-List
}

if ($Action -eq "stop") {
    Stop-ManagedProcess $FrontendPidPath
    Stop-ManagedProcess $BackendPidPath
    Write-Host "UAV operations console stopped."
    exit 0
}

if ($Action -eq "status") {
    Show-Status
    exit 0
}

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
if ((Test-LocalPort $BackendPort) -or (Test-LocalPort $FrontendPort)) {
    throw "Port $BackendPort or $FrontendPort is already in use. Run '.\\scripts\\console.ps1 status' first."
}

$python = Resolve-BackendPython
$backend = Start-Process -FilePath $python `
    -ArgumentList @("main.py", "--steps", "$Steps", "--hold-server", "--port", "$BackendPort") `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $BackendLogPath `
    -RedirectStandardError $BackendErrorPath `
    -WindowStyle Hidden `
    -PassThru
Set-Content -LiteralPath $BackendPidPath -Value $backend.Id -NoNewline

try {
    # The LongCat probe executes before the FastAPI listener opens.
    Wait-ForPort $BackendPort 45 "Backend"
    $frontend = Start-Process -FilePath $env:ComSpec `
        -ArgumentList @("/d", "/c", "npm run dev -- --host 127.0.0.1 --port $FrontendPort") `
        -WorkingDirectory (Join-Path $ProjectRoot "src\\vis\\frontend") `
        -RedirectStandardOutput $FrontendLogPath `
        -RedirectStandardError $FrontendErrorPath `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $FrontendPidPath -Value $frontend.Id -NoNewline
    Wait-ForPort $FrontendPort 20 "Frontend"
} catch {
    Stop-ManagedProcess $FrontendPidPath
    Stop-ManagedProcess $BackendPidPath
    throw $_
}

Write-Host "UAV operations console is running at http://127.0.0.1:$FrontendPort"
Write-Host "Logs: $RuntimeDir"
Show-Status
