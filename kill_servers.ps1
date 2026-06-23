Get-NetTCPConnection -LocalPort 8765,8766 -ErrorAction SilentlyContinue | Where-Object State -eq Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
