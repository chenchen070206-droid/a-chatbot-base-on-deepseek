# 一键停止 wc-chatbot 的脚本（由 stop_bot.bat 调用）
#   stop_bot.bat          只停脚本
#   stop_bot.bat wechat   停完脚本顺带关掉微信
param([string]$Mode = "")

$ErrorActionPreference = "SilentlyContinue"
$patterns = @("wx_multi.py", "send_guard.py", "broadcast.py", "vision_watch.py", "wx_chat_loop.py")

function Get-BotProcs {
    Get-CimInstance Win32_Process | Where-Object {
        if ($_.Name -notmatch '^(python|pythonw)\.exe$') { return $false }
        $cmd = $_.CommandLine
        if (-not $cmd) { return $false }
        foreach ($pat in $patterns) {
            if ($cmd -like "*$pat*") { return $true }
        }
        return $false
    }
}

Write-Host "正在停止脚本..."
$procs = @(Get-BotProcs)
if ($procs.Count -eq 0) {
    Write-Host "  没有在跑的脚本"
} else {
    foreach ($p in $procs) {
        $cmd = ($p.CommandLine -replace '\s+', ' ')
        if ($cmd.Length -gt 70) { $cmd = $cmd.Substring(0, 70) }
        Write-Host ("  停止 pid {0}  {1}" -f $p.ProcessId, $cmd)
        Stop-Process -Id $p.ProcessId -Force
    }
}

Start-Sleep -Seconds 2
$left = @(Get-BotProcs).Count
if ($left -eq 0) {
    Write-Host "  剩余脚本进程: 0（已全部停止）"
} else {
    Write-Host ("  剩余脚本进程: {0}（没退干净，再跑一次）" -f $left)
}

if ($Mode -ieq "wechat") {
    $w = @(Get-Process Weixin)
    if ($w.Count -eq 0) {
        Write-Host "  微信本来就没开"
    } else {
        $w | Stop-Process -Force
        Start-Sleep -Seconds 2
        Write-Host ("  微信剩余进程: {0}" -f @(Get-Process Weixin).Count)
    }
}

Write-Host "完成。"
