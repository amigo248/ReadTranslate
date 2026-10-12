$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run setup.ps1 first.' }
& $taskPython tools/make_icon.py
# Build away from the live library: PyInstaller replaces its collection folder.
$taskStage = Join-Path $PSScriptRoot 'build\portable-release'
& $taskPython -m PyInstaller --noconfirm --distpath $taskStage --workpath build '阅译.spec'
if ($LASTEXITCODE -ne 0) { throw 'Build failed. Review the terminal output.' }
$taskPackage = Join-Path $taskStage '阅译'
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'engines\manga\manifest.json'))) { throw 'Restore the owned engines from dist/阅译/engines first.' }
& $taskPython tools/assemble_standalone.py $taskPackage
if ($LASTEXITCODE -ne 0) { throw 'Standalone payload assembly failed.' }
& $taskPython tools/install_runtime_update.py --stage $taskPackage
if ($LASTEXITCODE -ne 0) { throw 'Close the reader and retry runtime installation.' }
& $taskPython tools/package_portable.py --models
if ($LASTEXITCODE -ne 0) { throw 'Portable package assembly failed.' }
Write-Host 'Build ready: dist\阅译\阅译.exe'
