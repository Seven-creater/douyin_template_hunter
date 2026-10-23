# 只终止 hunter 专用 profile 的 Edge 进程(按命令行匹配),不影响用户日常 Edge
$procs = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
    Where-Object { $_.CommandLine -like '*edge_dy_hunter*' }
if (-not $procs) {
    Write-Output "no hunter edge running"
} else {
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Output ("killed " + $p.ProcessId)
        } catch {}
    }
}
