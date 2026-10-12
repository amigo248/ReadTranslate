$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the local Python environment.' }
}
& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Unable to update pip in the local environment.' }
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-tested.txt 'setuptools>=71' wheel
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host 'Dependencies ready. Restore portable assets before using OCR/repair/upscaling; see README.md.'
