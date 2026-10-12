"""Rebuild only reader modules using an existing verified PyInstaller runtime.

Keeps third-party compiled code, bootstrap entries, Windows resources and the
shared dependency directory intact. Python bytecode version must match exactly.
"""
import argparse
import importlib.util
import marshal
import os
from pathlib import Path
import shutil
import struct
import types
import zlib
from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.archive.writers import CArchiveWriter
from PyInstaller.utils.win32.winutils import update_exe_pe_checksum

ROOT=Path(__file__).resolve().parents[1]


def signature(code):
    if not isinstance(code,types.CodeType):return code
    return (code.co_code,code.co_names,code.co_varnames,code.co_argcount,code.co_posonlyargcount,
            code.co_kwonlyargcount,code.co_freevars,code.co_cellvars,tuple(signature(v) for v in code.co_consts))


class FrozenEntryWriter(CArchiveWriter):
    def _write_entry(self,fp,entry):
        name,payload,compress,kind=entry
        if kind=='z':return self._write_file(fp,payload,name,kind,compress=compress)
        # Bootloader scripts/modules are already compiled with the exact ABI.
        return self._write_blob(fp,payload,name,kind,compress=compress)


def rebuild(installed,destination):
    installed=installed.resolve();destination=destination.resolve()
    assert (installed.is_relative_to(ROOT) or installed==ROOT.parent/'dist/阅译') and destination.is_relative_to(ROOT)
    destination.mkdir(parents=True,exist_ok=True)
    original=CArchiveReader(str(installed/'阅译.exe'))
    embedded=original.open_embedded_archive('PYZ.pyz')
    source=Path(embedded._filename)
    with source.open('rb') as stream:
        stream.seek(embedded._start_offset)
        header=stream.read(17)
    assert header[:8]==b'PYZ\0'+importlib.util.MAGIC_NUMBER,'Python bytecode versions differ'
    archive=destination/'PYZ.pyz'
    modules={}
    for path in (ROOT/'reader').glob('*.py'):
        if path.stem=='mtu_worker':continue # separate Python 3.12 sidecar
        name='reader' if path.stem=='__init__' else 'reader.'+path.stem
        modules[name]=path
    toc=[]
    with archive.open('wb') as out,source.open('rb') as old:
        out.write(header)
        for name,(kind,offset,length) in embedded.toc.items():
            if (name=='reader' or name.startswith('reader.')) and name not in modules:continue
            new_offset=out.tell()
            if name in modules:
                path=modules[name]
                code=compile(path.read_text('utf-8'),str(path.relative_to(ROOT)),'exec',optimize=0)
                blob=zlib.compress(marshal.dumps(code),6)
                kind=1 if path.stem=='__init__' else 0
            else:
                old.seek(embedded._start_offset+offset);blob=old.read(length)
            out.write(blob);toc.append((name,(kind,new_offset,len(blob))))
        for name in sorted(set(modules)-set(embedded.toc)):
            path=modules[name];code=compile(path.read_text('utf-8'),str(path.relative_to(ROOT)),'exec',optimize=0)
            blob=zlib.compress(marshal.dumps(code),6)
            toc.append((name,(1 if path.stem=='__init__' else 0,out.tell(),len(blob))));out.write(blob)
        offset=out.tell();out.write(marshal.dumps(toc))
        out.seek(8);out.write(struct.pack('!i',offset))
    for filename in ('阅译.exe','阅译OCR.exe'):
        reader=CArchiveReader(str(installed/filename))
        entries=[(option,b'',False,'o') for option in reader.options]
        for name,(_,_,_,compress,kind) in reader.toc.items():
            if name=='main' and kind=='s':
                payload=marshal.dumps(compile((ROOT/'main.py').read_text('utf-8'),'main.py','exec',optimize=0))
            else:
                payload=str(archive) if kind=='z' else reader.extract(name)
            entries.append((name,payload,bool(compress),kind))
        pkg=destination/(filename+'.pkg')
        FrozenEntryWriter(str(pkg),entries,'python313.dll')
        target=destination/filename
        with (installed/filename).open('rb') as old,target.open('wb') as out:
            out.write(old.read(reader._start_offset))
            with pkg.open('rb') as payload:shutil.copyfileobj(payload,out)
        update_exe_pe_checksum(str(target))
        rebuilt=CArchiveReader(str(target))
        assert signature(marshal.loads(rebuilt.extract('main')))==signature(compile((ROOT/'main.py').read_text('utf-8'),'main.py','exec',optimize=0))
        for name,(_,_,_,_,kind) in reader.toc.items():
            if name!='main' and kind!='z':assert rebuilt.extract(name)==reader.extract(name),name
        fresh=rebuilt.open_embedded_archive('PYZ.pyz')
        expected={name for name in embedded.toc if not (name=='reader' or name.startswith('reader.'))}|set(modules)
        assert set(fresh.toc)==expected
        for name,path in modules.items():
            assert signature(fresh.extract(name))==signature(compile(path.read_text('utf-8'),str(path),'exec',optimize=0)),name
        for name in embedded.toc:
            if not name.startswith('reader'):
                assert fresh.extract(name,raw=True)==embedded.extract(name,raw=True),name
        pkg.unlink()
        print(filename+': rebuilt reader modules; all third-party bytecode unchanged',flush=True)
    archive.unlink()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('installed',type=Path);parser.add_argument('destination',type=Path)
    args=parser.parse_args();rebuild(args.installed,args.destination)
