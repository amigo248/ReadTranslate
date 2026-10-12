"""Create a clean source archive and independent Windows release ZIP volumes.

Each ZIP is ordinary ZIP, under GitHub's per-asset limit. The included Windows
installer verifies and extracts all volumes without Python or third-party tools.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reader.config import Settings

LIMIT = 1700 * 1024 * 1024
MAX_ASSET = 2 * 1024**3
BLOCK = 4 * 1024 * 1024


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def scan_text(data, label):
    if re.search(rb'(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{24,}|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9]{25,})', data):
        raise ValueError('Possible credential in publication file: ' + label)


def source_archive(output):
    sources = []
    for folder in ('reader', 'tools', 'assets', 'licenses', 'vendor'):
        for path in (ROOT / folder).rglob('*'):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
                continue
            if path.name in ('.env', 'settings.json'):
                raise ValueError('Private file in source tree')
            sources.append(path)
    names = ('.gitignore', 'LICENSE', 'main.py', 'build.ps1', 'setup.ps1',
             'install-gpu.ps1', '启动阅译.cmd', '阅译.spec', 'pyproject.toml',
             'requirements.txt', 'requirements-tested.txt', 'README.md',
             'THIRD_PARTY.md', '漫画上色说明.md', '阅译目录说明.md', '发布说明.md')
    sources.extend(ROOT / name for name in names)
    package = output / '阅译-GitHub源码.zip'
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sorted(sources):
            relative = source.relative_to(ROOT).as_posix()
            if source.suffix.lower() in ('.py', '.md', '.json', '.txt', '.toml', '.ps1', '.cmd', '.spec'):
                scan_text(source.read_bytes(), relative)
            archive.write(source, relative)
    with zipfile.ZipFile(package) as archive:
        if archive.testzip() is not None:
            raise ValueError('Source archive CRC failed')
        for source in sources:
            if archive.read(source.relative_to(ROOT).as_posix()) != source.read_bytes():
                raise ValueError('Source archive differs from source files')
    return {'name': package.name, 'bytes': package.stat().st_size,
            'sha256': digest(package), 'files': len(sources)}


def payload(runtime, executables, fonts):
    runtime = runtime.resolve()
    records = {}
    for path in runtime.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(runtime).as_posix()
        if not path.resolve().is_relative_to(runtime):
            raise ValueError('Runtime link escaped source folder')
        parts = path.relative_to(runtime).parts
        if parts[0] not in ('_internal', 'engines', 'licenses', 'data'):
            continue
        if parts[0] == 'data' and (len(parts) < 3 or parts[1] != 'models'):
            continue
        if '__pycache__' in parts or '.locks' in parts or path.suffix in ('.pyc', '.pyo', '.log', '.lock'):
            continue
        if relative.startswith('engines/manga/fonts/'):
            continue  # Public release uses pinned OFL fonts, not workstation fonts.
        if path.name.startswith('.env') or path.name == 'settings.json':
            raise ValueError('Private configuration found in runtime assets: ' + relative)
        records[relative] = path
    for name in ('阅译.exe', '阅译OCR.exe'):
        records[name] = executables / name
    records['_internal/mtu_worker.py'] = ROOT / 'reader/mtu_worker.py'
    for source, name in (('README.md', '使用说明.md'), ('THIRD_PARTY.md', '第三方组件.md'),
                         ('漫画上色说明.md', '漫画上色说明.md'), ('LICENSE', 'LICENSE')):
        records[name] = ROOT / source
    records['先读我.md'] = ROOT / '发布说明.md'
    records['启动阅译.cmd'] = ('@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0"\r\nstart "" "%~dp0阅译.exe"\r\n').encode('utf-8')
    records['data/settings.json'] = encoded(asdict(Settings()))
    font_sources = json.loads((ROOT / 'assets/release-fonts.json').read_text('utf-8'))
    for item in font_sources:
        source = fonts / item['name']
        if source.stat().st_size != item['bytes'] or digest(source) != item['sha256']:
            raise ValueError('Public font checksum mismatch: ' + item['name'])
        records['engines/manga/fonts/' + item['name']] = source
    records['engines/manga/fonts/sources.json'] = encoded(font_sources)
    manifest = json.loads((runtime / 'engines/manga/manifest.json').read_text('utf-8'))
    manifest['files'] = [item for item in manifest['files'] if not item['path'].startswith('fonts/')]
    for relative, source in records.items():
        if relative.startswith('engines/manga/fonts/'):
            data = source.read_bytes() if isinstance(source, Path) else source
            manifest['files'].append({'path': relative.removeprefix('engines/manga/'),
                                      'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    manifest['provenance']['public_fonts'] = 'Noto Sans CJK SC Regular/Bold, SIL OFL 1.1; license included'
    records['engines/manga/manifest.json'] = encoded(manifest)
    for name, source in records.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name:
            raise ValueError('Unsafe archive path: ' + name)
        if isinstance(source, Path) and not source.is_file():
            raise ValueError('Release file missing: ' + str(source))
        if path.suffix in ('.py', '.json', '.md', '.txt', '.yaml', '.yml', '.ps1', '.cmd'):
            data = source.read_bytes() if isinstance(source, Path) else source
            scan_text(data, name)
    return records


def prepare(runtime, executables, fonts, output):
    output = output.resolve()
    if not output.is_relative_to(ROOT / 'releases') or output.exists():
        raise ValueError('Use a new directory beneath releases/')
    output.mkdir(parents=True)
    source = source_archive(output)
    records = payload(runtime, executables, fonts)
    parts, files = [], []
    archive, archive_path, current_size = None, None, 0

    def close_volume():
        nonlocal archive
        if archive is None:
            return
        archive.close()
        size = archive_path.stat().st_size
        if size >= MAX_ASSET:
            raise ValueError('ZIP exceeds GitHub release asset limit')
        parts.append({'name': archive_path.name, 'bytes': size, 'sha256': digest(archive_path)})
        print(f'Completed {archive_path.name}: {size / 1024**2:.1f} MiB', flush=True)
        archive = None

    def write_entry(name, source, include_inventory=True):
        nonlocal archive, archive_path, current_size
        size = source.stat().st_size if isinstance(source, Path) else len(source)
        if size > LIMIT:
            raise ValueError('Individual runtime file exceeds volume size')
        if archive is None or current_size + size > LIMIT:
            close_volume()
            archive_path = output / f'ReadTranslate-0.1.0-windows-x64-{len(parts) + 1:02d}.zip'
            archive = zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=5, allowZip64=False)
            current_size = 0
            print('Creating ' + archive_path.name, flush=True)
        algorithm = hashlib.sha256()
        with archive.open('阅译/' + name, 'w') as destination:
            if isinstance(source, Path):
                with source.open('rb') as stream:
                    while block := stream.read(BLOCK):
                        algorithm.update(block)
                        destination.write(block)
            else:
                algorithm.update(source)
                destination.write(source)
        current_size += size
        if include_inventory:
            files.append({'path': name, 'bytes': size, 'sha256': algorithm.hexdigest()})

    try:
        ordered = sorted(records, key=lambda name: (name.startswith(('engines/', 'data/models/')), name))
        for number, name in enumerate(ordered, 1):
            write_entry(name, records[name])
            if number % 4000 == 0:
                print(f'Packed {number}/{len(ordered)} files', flush=True)
        write_entry('release-files.json', encoded(files), include_inventory=False)
        close_volume()
    finally:
        if archive is not None:
            archive.close()
    release = {'format': 1, 'version': '0.1.0', 'platform': 'Windows x64',
               'parts': parts, 'source': source, 'runtime_files': len(files),
               'unpacked_bytes': sum(item['bytes'] for item in files),
               'settings': 'fresh defaults; no credentials, library or history',
               'fonts': 'Noto Sans CJK SC Regular/Bold; SIL OFL 1.1',
               'cloud_requests_during_build': 0}
    (output / 'release.json').write_bytes(encoded(release))
    for template, name in (('install-release.ps1', '安装阅译.ps1'), ('install-release.cmd', '安装阅译.cmd')):
        text = (ROOT / 'tools/templates' / template).read_text('utf-8-sig')
        (output / name).write_text(text, encoding='utf-8-sig' if name.endswith('.ps1') else 'utf-8')
    shutil.copy2(ROOT / '发布说明.md', output / '发布说明.md')
    checksums = [f'{digest(path)}  {path.name}' for path in sorted(output.iterdir()) if path.is_file()]
    (output / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', 'utf-8')
    print(json.dumps(release, ensure_ascii=False), flush=True)
    return release


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--executables', required=True, type=Path)
    parser.add_argument('--fonts', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    prepare(args.runtime, args.executables, args.fonts, args.output)
