@echo off
setlocal
powershell -NoProfile -Command "$ports=Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; foreach($entry in $ports){$process=Get-CimInstance Win32_Process -Filter ('ProcessId='+$entry.OwningProcess); if($process.CommandLine -like '*server.py*--port 8765*' -and $process.CommandLine -like '*127.0.0.1*'){Stop-Process -Id $entry.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Output 'Stopped the local SARATHI server.'}}"
endlocal

