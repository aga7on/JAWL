@echo off
setlocal
set "ROOT=%~dp0"

start "JAWL live log" powershell.exe -NoProfile -NoExit -Command "$Host.UI.RawUI.WindowTitle='JAWL live log'; Get-Content -LiteralPath '%ROOT%logs\startup\verification2_stderr.log' -Tail 80 -Wait"

start "QWB health" powershell.exe -NoProfile -NoExit -Command "$Host.UI.RawUI.WindowTitle='QWB health'; while ($true) { Clear-Host; Write-Host 'QWB /health'; Get-Date; try { Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 5 | ConvertTo-Json -Depth 6 } catch { Write-Host $_.Exception.Message -ForegroundColor Red }; Start-Sleep -Seconds 5 }"

endlocal
