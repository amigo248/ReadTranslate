param([string]$Destination = (Join-Path $PSScriptRoot '阅译'))
$ErrorActionPreference = 'Stop'
[AppContext]::SetSwitch('Switch.System.IO.UseLegacyPathHandling', $false)
[AppContext]::SetSwitch('Switch.System.IO.BlockLongPaths', $false)
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-LongPath([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if ($resolved.StartsWith('\\')) { return '\\?\UNC\' + $resolved.Substring(2) }
    return '\\?\' + $resolved
}

function Get-FileSHA256([string]$Path) {
    $stream = [IO.File]::OpenRead((Get-LongPath $Path))
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}

$release = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($release.format -ne 1 -or @($release.parts).Count -lt 1) { throw '发布清单格式无效。' }
$target = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $target) { throw '目标目录已存在。请选择一个新目录，避免覆盖已有书库。' }
$targetPrefix = $target + [IO.Path]::DirectorySeparatorChar

Write-Host '正在校验下载文件……'
foreach ($part in $release.parts) {
    if ([IO.Path]::GetFileName($part.name) -ne $part.name -or $part.sha256 -notmatch '^[0-9a-f]{64}$') { throw '压缩包清单无效。' }
    $path = Join-Path $PSScriptRoot $part.name
    if (-not (Test-Path -LiteralPath $path)) { throw ('缺少下载文件：' + $part.name) }
    if ((Get-Item -LiteralPath $path).Length -ne $part.bytes -or (Get-FileSHA256 $path) -ne $part.sha256) { throw ('文件不完整或已损坏：' + $part.name) }
}
[IO.Directory]::CreateDirectory((Get-LongPath $target)) | Out-Null
foreach ($part in $release.parts) {
    Write-Host ('正在解压：' + $part.name)
    $archive = [IO.Compression.ZipFile]::OpenRead((Get-LongPath (Join-Path $PSScriptRoot $part.name)))
    try {
        foreach ($entry in $archive.Entries) {
            if (-not $entry.FullName.StartsWith('阅译/')) { throw '压缩包目录结构无效。' }
            $relative = $entry.FullName.Substring(3).Replace('/', '\')
            if (-not $relative) { continue }
            $output = [IO.Path]::GetFullPath((Join-Path $target $relative))
            if (-not $output.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw '压缩包包含越界路径。' }
            if (-not $entry.Name) { [IO.Directory]::CreateDirectory((Get-LongPath $output)) | Out-Null; continue }
            [IO.Directory]::CreateDirectory((Get-LongPath ([IO.Path]::GetDirectoryName($output)))) | Out-Null
            $inputStream = $entry.Open()
            $outputStream = [IO.File]::Open((Get-LongPath $output), [IO.FileMode]::CreateNew)
            try { $inputStream.CopyTo($outputStream) }
            finally { $outputStream.Dispose(); $inputStream.Dispose() }
        }
    } finally { $archive.Dispose() }
}
Write-Host '正在校验解压结果……'
$files = Get-Content -LiteralPath (Join-Path $target 'release-files.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$number = 0
foreach ($file in $files) {
    $path = [IO.Path]::GetFullPath((Join-Path $target $file.path.Replace('/', '\')))
    if (-not $path.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw '文件校验清单包含越界路径。' }
    if ((Get-FileSHA256 $path) -ne $file.sha256) { throw ('解压校验失败：' + $file.path) }
    $number++
    if ($number % 2000 -eq 0) { Write-Host ("已校验 $number 个文件") }
}
if (-not [IO.File]::Exists((Get-LongPath (Join-Path $target '阅译.exe')))) { throw '程序文件缺失。' }
Write-Host ('完成。请双击：' + (Join-Path $target '启动阅译.cmd'))
