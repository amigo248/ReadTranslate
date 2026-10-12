"""Restore only runtime assets from a complete, extracted portable release."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil

ROOT = Path(__file__).resolve().parents[1]


def native_path(path):
    absolute = os.path.abspath(os.fspath(path))
    if os.name == 'nt' and not absolute.startswith('\\\\?\\'):
        absolute = '\\\\?\\UNC\\' + absolute[2:] if absolute.startswith('\\\\') else '\\\\?\\' + absolute
    return Path(absolute)


def digest(path):
    with native_path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def restore(portable):
    portable = portable.resolve()
    engine = portable / 'engines/manga'
    manifest = json.loads(native_path(engine / 'manifest.json').read_text('utf-8'))
    items = []
    for record in manifest['files']:
        relative = Path(record['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe engine manifest path')
        source = (engine / relative).resolve()
        if not source.is_relative_to(engine.resolve()):
            raise ValueError('Engine asset escaped its directory')
        if native_path(source).stat().st_size != record['size'] or digest(source) != record['sha256']:
            raise ValueError('Portable asset checksum mismatch: ' + record['path'])
        items.append((source, ROOT / 'engines/manga' / relative))
    models = portable / 'data/models'
    if not models.is_dir():
        raise ValueError('Portable OCR models missing')
    for source in models.rglob('*'):
        if source.is_file() and '.locks' not in source.parts and source.suffix != '.lock':
            if not source.resolve().is_relative_to(models.resolve()):
                raise ValueError('OCR model escaped its directory')
            items.append((source, ROOT / 'data/models' / source.relative_to(models)))
    for source, target in items:
        if source == target.resolve():
            raise ValueError('Select a separate extracted portable directory')
    for number, (source, target) in enumerate(items, 1):
        native_path(target.parent).mkdir(parents=True, exist_ok=True)
        if native_path(target).is_file() and digest(target) == digest(source):
            continue
        temporary = target.with_name(target.name + '.restore-new')
        shutil.copy2(native_path(source), native_path(temporary))
        os.replace(native_path(temporary), native_path(target))  # Do not write through existing hard links.
        if number % 2000 == 0:
            print(f'{number}/{len(items)} assets restored', flush=True)
    shutil.copy2(native_path(engine / 'manifest.json'), native_path(ROOT / 'engines/manga/manifest.json'))
    print('Assets restored. Personal books, settings and credentials were not copied.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('portable', type=Path, help='Extracted folder containing 阅译.exe')
    restore(parser.parse_args().portable)
