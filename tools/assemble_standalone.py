"""Assemble an owned payload after PyInstaller has finished collecting files."""
import argparse
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent

def long_path(path):
    absolute=path.resolve()
    return Path('\\\\?\\'+str(absolute)) if os.name=='nt' else absolute

def copy_asset(source,target):
    """Replace shared weights atomically so updating one runtime leaves the other intact."""
    source=Path(source);target=Path(target)
    if target.is_file() and target.stat().st_nlink>1:
        staged=target.with_name(target.name+'.'+uuid.uuid4().hex+'.new')
        try:
            shutil.copy2(source,staged);os.replace(staged,target)
        finally:staged.unlink(missing_ok=True)
        return str(target)
    return shutil.copy2(source,target)

def assemble(destination):
    destination=destination.resolve()
    assert destination.is_relative_to(ROOT), destination
    assert (destination/'阅译.exe').is_file(), 'Finish PyInstaller before assembly'
    for engine in ('manga',):
        source=ROOT/'engines'/engine
        manifest=json.loads((source/'manifest.json').read_text('utf-8'))
        for item in manifest['files']:
            relative=Path(item['path'])
            assert not relative.is_absolute() and '..' not in relative.parts
            original=long_path(source/relative);target=long_path(destination/'engines'/engine/relative)
            target.parent.mkdir(parents=True,exist_ok=True)
            copy_asset(original,target)
            assert target.stat().st_size==item['size']
        shutil.copy2(source/'manifest.json',destination/'engines'/engine/'manifest.json')
        print(engine+' assembled',flush=True)
    shutil.copytree(ROOT/'data/models',destination/'data/models',dirs_exist_ok=True,copy_function=copy_asset,
                    ignore=shutil.ignore_patterns('.locks','*.lock'))
    print('Offline legacy OCR cache included',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('destination',type=Path)
    assemble(parser.parse_args().destination)
