# Starts Relay in the background (no window). Run again safely: a second copy exits.
# Python: $env:RELAY_PYTHON if set, else .venv in this folder, else pythonw on PATH.
$py = $env:RELAY_PYTHON
if (-not $py) {
  $venv = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
  $py = if (Test-Path $venv) { $venv } else { "pythonw" }
}
$py = $py -replace 'python\.exe$', 'pythonw.exe'
Start-Process -FilePath $py -ArgumentList "`"$PSScriptRoot\relay.py`"" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
