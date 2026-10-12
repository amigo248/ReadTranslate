"""Fetch pinned, hash-verified upscaling assets into the reader's owned engine."""
import ast
import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/'engines/manga'
OUT=ENGINE/'models/upscaling'

def constants(file,name):
    tree=ast.parse((ENGINE/'manga_translator/upscaling'/file).read_text('utf-8'))
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets))

def digest(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def fetch(path,urls,sha):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and digest(path)==sha:return
    temporary=path.with_suffix(path.suffix+'.download')
    for url in urls:
        try:
            print('Downloading '+path.name,flush=True)
            request=urllib.request.Request(url,headers={'User-Agent':'ReadTranslate/1.0'})
            with urllib.request.urlopen(request,timeout=90) as response,temporary.open('wb') as stream:
                shutil.copyfileobj(response,stream,1024*1024)
            if digest(temporary)!=sha:raise ValueError('SHA256 mismatch: '+path.name)
            temporary.replace(path);return
        except Exception as e:print(type(e).__name__+': '+str(e),flush=True)
        finally:temporary.unlink(missing_ok=True)
    raise RuntimeError('Unable to acquire verified asset: '+path.name)

def main():
    records=[]
    archive=ROOT/'build/realesrgan-ncnn-vulkan-20220424-windows.zip'
    urls=['https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/'+archive.name,
          'https://www.modelscope.cn/models/hgmzhn/manga-translator-ui/resolve/master/'+archive.name]
    sha='abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d'
    fetch(archive,urls,sha)
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            rel=Path(info.filename)
            if rel.is_absolute() or '..' in rel.parts:raise ValueError('Unsafe archive path')
            if info.is_dir():continue
            destination=OUT/'esrgan-win'/rel;destination.parent.mkdir(parents=True,exist_ok=True)
            with package.open(info) as source,destination.open('wb') as stream:shutil.copyfileobj(source,stream)
    records.append({'name':archive.name,'urls':urls,'sha256':sha})
    groups=[('realcugan','realcugan.py','_MODEL_HASHES',[f'up{r}x-latest-conservative.pth' for r in (2,3,4)],'v1.8.0'),
            ('mangajanai','mangajanai.py','_KNOWN_MODELS',[
              '2x_MangaJaNai_1600p_V1_ESRGAN_90k.pth','4x_MangaJaNai_1600p_V1_ESRGAN_70k.pth',
              '2x_IllustrationJaNai_V1_ESRGAN_120k.pth','4x_IllustrationJaNai_V1_ESRGAN_135k.pth'],'v1.9.5')]
    for folder,file,key,names,release in groups:
        hashes=constants(file,key)
        for name in names:
            urls=['https://github.com/hgmzhn/manga-translator-ui/releases/download/'+release+'/'+name,
                  'https://www.modelscope.cn/models/hgmzhn/manga-translator-ui/resolve/master/'+name]
            fetch(OUT/folder/name,urls,hashes[name]);records.append({'name':folder+'/'+name,'urls':urls,'sha256':hashes[name]})
    (OUT/'sources.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),'utf-8')
    # Update only new assets; retain all existing engine provenance and hashes.
    path=ENGINE/'manifest.json';manifest=json.loads(path.read_text('utf-8'))
    existing={r['path']:r for r in manifest['files']}
    for asset in OUT.rglob('*'):
        if asset.is_file():existing[asset.relative_to(ENGINE).as_posix()]={'path':asset.relative_to(ENGINE).as_posix(),'size':asset.stat().st_size,'sha256':digest(asset)}
    manifest['files']=sorted(existing.values(),key=lambda r:r['path'])
    path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
    print('All three owned upscaling engines acquired and verified',flush=True)

if __name__=='__main__':main()
