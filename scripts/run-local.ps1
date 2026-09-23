$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    throw 'Сначала создайте .venv и установите requirements.txt по README.'
}
& .\.venv\Scripts\python.exe manage.py migrate
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& .\.venv\Scripts\python.exe manage.py bootstrap_demo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& .\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --noreload

