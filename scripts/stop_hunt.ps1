# 停掉所有 hunter 相关 python 进程(按命令行匹配,不看启动时间——
# 防止旧注册表的僵尸批量混进新批量)
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'hunter|scripts' }
if (-not $procs) {
    Write-Output "no hunter python running"
} else {
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force
            Write-Output ("stopped python " + $p.ProcessId)
        } catch {}
    }
}
