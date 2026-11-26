# Stops Relay.
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
  Where-Object { $_.CommandLine -match 'relay\\relay.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
