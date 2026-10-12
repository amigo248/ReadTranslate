"""Owned super-resolution, derivative editions and original-coordinate OCR."""
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
import hashlib,json,os,shutil,subprocess,tempfile,time,uuid

from PIL import Image
from .config import write_json
from .images import load_image,render_page
from .pipeline import edit_path,translated_path,translate_book

ENGINES=(('Real-ESRGAN','esrgan'),('Real-CUGAN','realcugan'),('MangaJaNai','mangajanai'))
MODES=(('仅超分','only'),('先超分后翻译','before'))

def options(settings):
    result={k:getattr(settings,'upscale_'+k) for k in ('engine','ratio','mode','tile')}
    if result['engine'] not in {v for _,v in ENGINES} or result['ratio'] not in (2,3,4) or result['mode'] not in {v for _,v in MODES} or result['tile'] not in (128,256,400):
        raise ValueError('超分设置无效，请重新选择方案、倍率和分块大小')
    return result

@lru_cache(maxsize=1024)
def _hash(path,size,modified):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def fingerprint(path):
    stat=path.stat();return _hash(str(path.resolve()),stat.st_size,stat.st_mtime_ns)

def dependencies(library,book,index,kind):
    source=library.path(book)/library.manifest(book)['units'][index]['path']
    result={'source':fingerprint(source)}
    if kind=='translated':
        target=translated_path(library,book,index);edit=edit_path(library,book,index)
        if not target.exists():return None
        result.update(translated=fingerprint(target),edits=fingerprint(edit) if edit.exists() else '')
    return result

def edition_path(library,book,index,kind):
    return library.path(book)/'upscaled'/kind/f'{index:06d}.png'

def available_edition(library,book,index,kind,expected=None):
    path=edition_path(library,book,index,kind)
    try:
        if kind=='translated':
            from .edition_review import record,valid_image
            own=record(library,book,index,'upscaled_translated')
            if own:
                if expected is not None and own.get('recipe')!=expected:return None
                return valid_image(library,book,index,'upscaled_translated')
        data=json.loads(path.with_suffix('.json').read_text('utf-8'))
        if not path.is_file() or data['dependencies']!=dependencies(library,book,index,kind) or data.get('output_sha256')!=fingerprint(path):return None
        if expected is not None and data['options']!=expected:return None
        return path
    except (OSError,ValueError,KeyError):return None

def upscale_image(library,source,destination,settings,cancel=lambda:False):
    settings_options=options(settings)
    if cancel():raise InterruptedError('超分已暂停')
    image=load_image(source)
    if image.width*image.height*settings_options['ratio']**2>80_000_000:
        raise ValueError('超分结果超过 8000 万像素，请降低倍率或先裁剪页面')
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.stem+'-'+uuid.uuid4().hex+'.tmp.png')
    from .mtu_bridge import MTUProcess
    process=None
    try:
        if settings_options['engine']=='esrgan':
            from .embedded_runtime import manga_root,child_environment,isolated_dll_search
            engine=manga_root()/'models/upscaling/esrgan-win';exe=engine/'realesrgan-ncnn-vulkan.exe'
            if not exe.is_file() or not (engine/'models/realesr-animevideov3-x2.bin').is_file():raise ValueError('内置 Real-ESRGAN 组件缺失，请恢复 engines 文件夹')
            with (library.root/'upscaling.log').open('a',encoding='utf-8') as log,isolated_dll_search():
                worker=subprocess.Popen([str(exe),'-i',str(Path(source).resolve()),'-o',str(temporary.resolve()),'-m',str(engine/'models'),
                    '-s',str(settings_options['ratio']),'-t',str(settings_options['tile']),'-j','1:1:1'],
                    cwd=engine,stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=child_environment(),
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                try:
                    while worker.poll() is None:
                        if cancel():raise InterruptedError('超分已暂停，旧结果保留')
                        time.sleep(.1)
                    if worker.returncode:raise ValueError('Real-ESRGAN 未完成，请检查显卡驱动及 upscaling.log')
                finally:
                    if worker.poll() is None:worker.terminate();worker.wait(timeout=10)
        else:
            process=MTUProcess(library,cancel)
            process.request('upscale',path=str(Path(source).resolve()),output=str(temporary.resolve()),gpu=settings.gpu,
                            engine=settings_options['engine'],ratio=settings_options['ratio'],tile=settings_options['tile'])
        if cancel():raise InterruptedError('超分已暂停，旧结果保留')
        with Image.open(temporary) as output:
            if output.size!=(image.width*settings_options['ratio'],image.height*settings_options['ratio']):raise ValueError('超分输出尺寸不正确，旧结果保留')
            output.verify()
        temporary.replace(destination)
    finally:
        if process:process.close()
        temporary.unlink(missing_ok=True)

def scale_edits(data,factor):
    """Never store scaled coordinates over the user's original-coordinate edits."""
    result=deepcopy(data)
    def visit(value):
        if isinstance(value,list):
            for item in value:visit(item)
        elif isinstance(value,dict):
            for key,item in list(value.items()):
                if (key=='box' or key.endswith('_box')) and isinstance(item,list) and len(item)==4 and all(isinstance(n,(int,float)) for n in item):
                    value[key]=[round(n*factor) for n in item]
                    value[key][2:]=[max(1,n) for n in value[key][2:]]
                elif key=='text_polygons':value[key]=[[[n*factor for n in point] for point in polygon] for polygon in item]
                elif key in ('font_size','source_font_size','render_font_size','size') and isinstance(item,(int,float)):value[key]=round(item*factor) if item else 0
                elif key=='points':value[key]=[[n*factor for n in point] for point in item]
                else:visit(item)
            if 'text' in value and 'box' in value:value['render_scale']=max(1,factor)
    visit(result)
    for key in ('width','height'):
        if key in result:result[key]=round(result[key]*factor)
    if result.get('residual_cleanup'):result['residual_cleanup']['coordinate_scale']=max(1,factor)
    return result

class UpscaledOCR:
    def __init__(self,base,library,settings,cancel):
        self.base,self.library,self.settings,self.cancel=base,library,settings,cancel
    def recognize(self,source,output,gpu=True):
        source=Path(source);cache=self.library.root/'upscale-ocr';cache.mkdir(exist_ok=True)
        enlarged=ocr_image_path(self.library,source,self.settings);key=enlarged.stem
        if not enlarged.exists():upscale_image(self.library,source,enlarged,self.settings,self.cancel)
        with tempfile.TemporaryDirectory(prefix='ocr-',dir=cache) as temporary:
            high=Path(temporary)/'ocr.json';self.base.recognize(enlarged,high,gpu)
            data=scale_edits(json.loads(high.read_text('utf-8')),1/self.settings.upscale_ratio)
            size=load_image(source).size;data.update(width=size[0],height=size[1],upscale_ocr=options(self.settings))
            for region in data.get('regions',[]):
                for name in ('mtu_mask','mtu_bubble'):
                    if region.get(name) and Path(region[name]).exists():
                        small=cache/(key+'-'+name+'.png')
                        with Image.open(region[name]) as image:image.resize(size,Image.Resampling.NEAREST).save(small)
                        region[name]=str(small)
            write_json(Path(output),data)
    def supplement_white(self,*args):return self.base.supplement_white(*args)
    def request(self,*args,**kwargs):return self.base.request(*args,**kwargs)
    def close(self):self.base.close()

def ocr_image_path(library,source,settings):
    key=hashlib.sha256((fingerprint(Path(source))+json.dumps(options(settings),sort_keys=True)).encode()).hexdigest()
    return library.root/'upscale-ocr'/(key+'.png')

def publish(library,book,index,kind,source,settings,expected):
    """Publish an image and provenance together, preserving the previous edition."""
    if dependencies(library,book,index,kind)!=expected:raise ValueError('页面在处理期间发生变化，请重新执行超分')
    target=edition_path(library,book,index,kind);meta=target.with_suffix('.json')
    target.parent.mkdir(parents=True,exist_ok=True)
    backup=library.path(book)/'backups'/('upscale-'+uuid.uuid4().hex)
    old={p:p.exists() for p in (target,meta)}
    if any(old.values()):
        backup.mkdir(parents=True)
        for path,exists in old.items():
            if exists:shutil.copy2(path,backup/path.name)
    try:
        staged=target.with_name(target.stem+'-'+uuid.uuid4().hex+'.tmp.png')
        shutil.copy2(source,staged);staged.replace(target)
        write_json(meta,{'dependencies':expected,'options':options(settings),'output_sha256':fingerprint(target)})
        if kind=='translated':
            from .edition_review import isolated,capture_generated,load_data
            if isolated(library,book,index):capture_generated(library,book,index,'upscaled_translated',target,load_data(library,book,index,'translated'),options(settings))
    except Exception:
        for path,exists in old.items():
            if exists:shutil.copy2(backup/path.name,path)
            else:path.unlink(missing_ok=True)
        raise

def upscale_saved_translation(library,book,index,settings,cancel):
    """Enlarge the original then typeset saved Chinese; never detect/OCR/translate."""
    from .edition_review import isolated,load_data,record,render_review
    if isolated(library,book,index):
        data=load_data(library,book,index,'upscaled_translated' if record(library,book,index,'upscaled_translated') else 'translated')
        source=library.path(book)/library.manifest(book)['units'][index]['path']
        enlarged=ocr_image_path(library,source,settings);enlarged.parent.mkdir(parents=True,exist_ok=True)
        if not enlarged.is_file():
            owned=available_edition(library,book,index,'original',dict(options(settings),mode='only'))
            if owned:shutil.copy2(owned,enlarged)
            else:upscale_image(library,source,enlarged,settings,cancel)
        return render_review(library,book,index,'upscaled_translated',data,settings,cancel=cancel,recipe=options(settings),regenerate=True)
    from .page_render import stage_page_editions,commit_page_editions
    expected=dependencies(library,book,index,'translated')
    data=json.loads(edit_path(library,book,index).read_text('utf-8'))
    with tempfile.TemporaryDirectory(prefix='upscale-saved-',dir=library.path(book)) as folder:
        def stage(backend=None):
            return stage_page_editions(library,book,index,data,settings,translated_path(library,book,index),folder,
                                       cancel=cancel,backend=backend,profile=options(settings))
        if any(r.get('processing_engine')=='mtu' for r in data.get('regions',[])):
            from .render_pool import render_process
            with render_process(library,cancel,settings.gpu) as backend:outputs=stage(backend)
        else:outputs=stage()
        if cancel():raise InterruptedError('超分已暂停，旧结果保留')
        if dependencies(library,book,index,'translated')!=expected:raise ValueError('页面在处理期间发生变化，请重试超分')
        backup=library.path(book)/'backups'/('upscale-'+uuid.uuid4().hex)
        for target,staged in outputs:
            if target.is_file() and fingerprint(target)!=fingerprint(Path(staged)):
                saved=backup/target.relative_to(library.path(book))
                saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved)
        commit_page_editions(outputs,folder)


def upscale_book(library,book,settings,indexes,progress=lambda *_:None,cancel=lambda:False,job_id=None,translator=None,source_kind=None):
    if source_kind is not None:
        if source_kind not in ('original','translated'):raise ValueError('超分来源无效')
        settings=replace(settings,upscale_mode='only' if source_kind=='original' else 'before')
    opts=options(settings);manifest=library.manifest(book);root=library.path(book)
    if manifest['kind']!='comic':raise ValueError('超分仅用于漫画')
    if not indexes or any(not isinstance(i,int) or i<0 or i>=len(manifest['units']) for i in indexes):raise ValueError('超分页码范围无效')
    kind='original' if opts['mode']=='only' else 'translated'
    if source_kind=='translated':
        for index in indexes:
            if not translated_path(library,book,index).is_file() or not edit_path(library,book,index).is_file():
                raise ValueError(f'第 {index+1} 页尚未生成中文版，请先翻译或校对生成译图；超分不会自动翻译')
    payload={'indexes':indexes,'options':opts}
    if source_kind is not None:payload['source_kind']=source_kind
    job_id=job_id or library.create_job(book,'upscale',len(indexes),payload)
    done=0;layout_warnings=0
    try:
        for index in indexes:
            if cancel():raise InterruptedError('超分已暂停，完成的页面已保存')
            from .edition_review import isolated
            saved_chinese=kind=='translated' and isolated(library,book,index) and translated_path(library,book,index).is_file() and edit_path(library,book,index).is_file()
            label='仅超分' if source_kind is not None or saved_chinese else dict((v,k) for k,v in MODES)[opts['mode']]
            progress(done,len(indexes),f'第 {index+1} 页：{label} · {opts["ratio"]} 倍')
            if (source_kind=='translated' or saved_chinese) and not available_edition(library,book,index,kind,opts):
                upscale_saved_translation(library,book,index,settings,cancel)
            elif not available_edition(library,book,index,kind,opts):
                source=root/manifest['units'][index]['path'];source_hash=fingerprint(source)
                with tempfile.TemporaryDirectory(prefix='upscale-',dir=root) as temporary:
                    folder=Path(temporary);result=folder/'result.png'
                    expected=dependencies(library,book,index,kind)
                    if opts['mode']=='only':upscale_image(library,source,result,settings,cancel)
                    else:
                        translate_book(library,book,settings,[index],progress=lambda a,b,m:progress(done,len(indexes),m),cancel=cancel,translator=translator,reprocess=True,
                            ocr_factory=lambda base:UpscaledOCR(base,library,settings,cancel),manage_job=False,preserve_upscale=False)
                        expected=dependencies(library,book,index,kind)
                        enlarged=ocr_image_path(library,source,settings)
                        if not enlarged.exists():upscale_image(library,source,enlarged,settings,cancel)
                        data=scale_edits(json.loads(edit_path(library,book,index).read_text('utf-8')),opts['ratio'])
                        # A mask generated at original resolution may inform erasure after resizing.
                        from .page_render import render_context
                        context=render_context(library,book,index,'enhanced',folder)
                        def render_high(backend=None):
                            render_page(enlarged,data['regions'],result,settings.font_path,settings.comic_scope,settings.comic_layout,
                                        data.get('manual_selections'),settings.comic_style,settings=settings,
                                        cleanup=data.get('residual_cleanup'),backend=backend,incremental=context)
                        if any(r.get('processing_engine')=='mtu' for r in data['regions']):
                            from .render_pool import render_process
                            with render_process(library,cancel,settings.gpu) as backend:render_high(backend)
                        else:render_high()
                        from .layout_status import overflow_count,mark_overflow
                        if overflow_count(data):
                            if dependencies(library,book,index,kind)!=expected:raise ValueError('页面在处理期间发生变化，请重试超分')
                            original_edits=json.loads(edit_path(library,book,index).read_text('utf-8'))
                            pairs=list(zip(original_edits.get('regions',[]),data.get('regions',[])))
                            pairs.extend((left,right) for a,b in zip(original_edits.get('manual_selections',[]),data.get('manual_selections',[]))
                                         for left,right in zip(a.get('regions',[]),b.get('regions',[])))
                            changed=False
                            for original_region,scaled_region in pairs:
                                issue=scaled_region.get('render_overflow')
                                if issue and not original_region.get('render_overflow'):
                                    mark_overflow(original_region,'超分译本：'+issue['message'],original_region['box']);changed=True
                            if changed:
                                write_json(edit_path(library,book,index),original_edits)
                                expected=dependencies(library,book,index,kind)
                    if cancel():raise InterruptedError('超分已暂停，旧超分结果保留')
                    if fingerprint(source)!=source_hash:raise ValueError('原图在处理期间发生变化，请重试')
                    publish(library,book,index,kind,result,settings,expected)
                    if opts['mode']=='before' and context['staged'].is_file():
                        # Advisory cache is published only after the visible image.
                        # A cache write failure must not undo a completed translation.
                        try:write_json(context['state'],json.loads(context['staged'].read_text('utf-8')))
                        except OSError:pass
            from .colorization import sync_existing_colors,page_edits
            sync_existing_colors(library,book,index,page_edits(library,book,index))
            from .layout_status import overflow_count
            edit=edit_path(library,book,index)
            pending=overflow_count(json.loads(edit.read_text('utf-8'))) if kind=='translated' and edit.exists() else 0
            layout_warnings+=pending
            done+=1;library.update_job(job_id,done,message=f'完成第 {index+1} 页'+(f'；{pending} 个译文放不下，已标红待校对' if pending else ''))
            progress(done,len(indexes),f'完成第 {index+1} 页'+(f'；{pending} 个译文放不下，已标红待校对' if pending else ''))
        library.update_job(job_id,done,'complete','超分完成'+(f'；{layout_warnings} 个译文放不下，已标红待校对' if layout_warnings else ''));return book
    except InterruptedError as error:
        library.update_job(job_id,done,'paused',str(error));raise
    except Exception as error:
        library.update_job(job_id,done,'failed',str(error));raise

def verify_owned():
    """Diagnostics in a caller-selected private library; no OCR or cloud requests."""
    from .config import app_root,data_root,Settings
    from .library import Library
    from .mtu_bridge import MTUProcess
    root=app_root().resolve();library=Library(data_root());source=Path(os.environ['READ_TRANSLATE_UPSCALE_INPUT'])
    before=fingerprint(source);report=[]
    worker=MTUProcess(library)
    try:info=worker.request('info')
    finally:worker.close()
    for field in ('executable','prefix','torch_path','engine_root'):assert Path(info[field]).resolve().is_relative_to(root),info
    for engine in ('esrgan','realcugan','mangajanai'):
        target=library.root/(engine+'.png');upscale_image(library,source,target,Settings(upscale_engine=engine))
        report.append({'engine':engine,'size':load_image(target).size})
    # Color model routing and CPU fallback also run on owned weights.
    color=library.root/'color.png';Image.new('RGB',(80,96),(40,120,220)).save(color)
    for ratio in (2,4):
        target=library.root/f'illustration-{ratio}.png'
        upscale_image(library,color,target,Settings(upscale_engine='mangajanai',upscale_ratio=ratio))
        report.append({'engine':'IllustrationJaNai','ratio':ratio,'size':load_image(target).size})
    for engine in ('realcugan','mangajanai'):
        target=library.root/(engine+'-cpu.png');upscale_image(library,source,target,Settings(upscale_engine=engine,gpu=False))
        report.append({'engine':engine,'cpu':True,'size':load_image(target).size})
    assert fingerprint(source)==before
    write_json(library.root/'upscale-standalone-result.json',{'root':str(root),'engines':report,'owned_runtime':info,'cloud_requests':0,'input_preserved':True})
    print(json.dumps(report),flush=True)
