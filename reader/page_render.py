"""Regenerate ordinary and enhanced editions from original-coordinate edits."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json, os, shutil, tempfile,time
from PIL import Image

from .config import write_json
from .images import load_image, render_page


def render_context(library,book,index,kind,directory):
    from .pipeline import translated_path
    from .upscaling import edition_path
    return {'state':library.path(book)/'cache/page-state'/f'{index:06d}-{kind}.json',
            'staged':Path(directory)/(kind+'-state.json'),
            'previous':translated_path(library,book,index) if kind=='normal' else edition_path(library,book,index,'translated'),
            'metrics':{}}


def saved_page_current(library,book,index,edits,settings,preserve_upscale=True):
    """Skip completed pages only when pixels, settings and saved edits match."""
    from .incremental_render import geometry_key,load_state
    from .cleanup import validate_cleanup
    from .upscaling import available_edition
    if not any(r.get('processing_engine')=='mtu' for r in edits.get('regions',[])):return False
    if any(not r.get('balloon_analysis')==4 or r.get('analysis_box')!=r['box'] for r in edits.get('regions',[])):return False
    if edits.get('boundary_version',0)<3 or edits.get('white_scan_version',0)<1:return False
    cleanup=validate_cleanup(edits.get('residual_cleanup'))
    cleanup.setdefault('strength',settings.comic_cleanup_strength)
    if cleanup.get('strokes'):return False
    source=library.path(book)/library.manifest(book)['units'][index]['path']
    regions=edits.get('regions',[])
    key=geometry_key(source,regions,settings.comic_scope,cleanup,edits.get('manual_selections'))
    from .edition_review import isolated,context
    ctx=context(library,book,index,'translated',library.root) if isolated(library,book,index) else render_context(library,book,index,'normal',library.root)
    state=load_state(ctx,key,regions,settings.font_path,settings.comic_layout)
    if not state or state['dirty']:return False
    if preserve_upscale and upscale_profile(library,book,index):
        if not available_edition(library,book,index,'translated'):return False
    return True


def upscale_profile(library, book, index, preferred_kind=None):
    """Edits may already be saved: recover the page's recipe, not its stale pixels."""
    from .upscaling import edition_path, fingerprint, options
    source = library.path(book)/library.manifest(book)['units'][index]['path']
    kinds=('original','translated') if preferred_kind=='original' else ('translated','original')
    for kind in kinds:
        path = edition_path(library, book, index, kind)
        try:
            meta = json.loads(path.with_suffix('.json').read_text('utf-8'))
            if meta['dependencies']['source'] != fingerprint(source):
                continue
            saved = meta['options']
            from .config import Settings
            settings = replace(Settings(), **{'upscale_'+k: saved[k] for k in ('engine','ratio','mode','tile')})
            saved = options(settings)
            if kind == 'translated' and saved['mode'] == 'only':
                continue
            if not path.is_file() or meta['output_sha256'] != fingerprint(path):
                continue
            size = load_image(source).size
            with Image.open(path) as image:
                if image.size != tuple(n*saved['ratio'] for n in size):
                    continue
            return saved
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def _copy_render_status(original, scaled, ratio):
    from .upscaling import scale_edits
    normalized = scale_edits(scaled, 1/ratio)
    pairs = list(zip(original.get('regions',[]), normalized.get('regions',[])))
    for left, right in zip(original.get('manual_selections',[]), normalized.get('manual_selections',[])):
        pairs.extend(zip(left.get('regions',[]), right.get('regions',[])))
    for left, right in pairs:
        # Geometry, manual font choices and reviewed translations stay in source coordinates.
        for key in ('render_overflow','render_warning','render_font_size','render_box'):
            left.pop(key, None)
            if key in right:
                left[key] = deepcopy(right[key])


def stage_page_editions(library, book, index, edits, settings, normal, directory,
                       *, cancel=lambda:False, backend=None, profile=None,metrics=None):
    """Stage enhanced image plus provenance before changing any visible edition."""
    from .pipeline import edit_path, translated_path
    from .upscaling import edition_path, fingerprint, upscale_image, ocr_image_path, scale_edits
    from .edition_review import isolated,folder,_record
    if isolated(library,book,index):
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
        data_path=directory/'edits.json';write_json(data_path,edits)
        rendered=directory/'rendered.json';write_json(rendered,edits)
        target=translated_path(library,book,index)
        meta=_record(library,book,index,'translated',normal,edits);meta['image']=target.relative_to(library.path(book)).as_posix()
        staged_meta=directory/'record.json';write_json(staged_meta,meta)
        destination=folder(library,book,index,'translated')
        outputs=[(target,Path(normal)),(edit_path(library,book,index),data_path),(destination/'record.json',staged_meta),(destination/'rendered.json',rendered)]
        ctx=render_context(library,book,index,'normal',directory)
        if ctx['staged'].is_file():outputs.append((ctx['state'],ctx['staged']))
        return outputs
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source = library.path(book)/library.manifest(book)['units'][index]['path']
    source_hash = fingerprint(source)
    profile = upscale_profile(library, book, index) if profile is None else profile
    extra = []
    if profile:
        saved = dict(profile)
        # A previously enlarged original becomes an enlarged translated edition.
        if saved['mode'] == 'only':
            saved['mode'] = 'before'
        effective = replace(settings, **{'upscale_'+k:v for k,v in saved.items()})
        enhanced = directory/'enhanced.png'
        if cancel():
            raise InterruptedError('生成已暂停，旧译图保留')
        enlarged = ocr_image_path(library, source, effective)
        enlarged.parent.mkdir(parents=True, exist_ok=True)
        if not enlarged.exists():
            # Use an already verified enhanced original when possible.
            from .upscaling import available_edition
            original_options = dict(saved, mode='only')
            owned = available_edition(library, book, index, 'original', original_options)
            if owned:
                shutil.copy2(owned, enlarged)
            else:
                upscale_image(library, source, enlarged, effective, cancel)
        expected_size = tuple(n*saved['ratio'] for n in load_image(source).size)
        if load_image(enlarged).size != expected_size:
            raise ValueError('超分底图尺寸错误，请重新执行超分；旧译图保留')
        scaled = scale_edits(edits, saved['ratio'])
        context=render_context(library,book,index,'enhanced',directory)
        started=time.perf_counter()
        render_page(enlarged, scaled.get('regions',[]), enhanced, settings.font_path,
                    settings.comic_scope, settings.comic_layout, scaled.get('manual_selections'),
                    settings.comic_style, settings=settings, cleanup=scaled.get('residual_cleanup'),
                    backend=backend,incremental=context)
        context['metrics']['seconds']=round(time.perf_counter()-started,3)
        if metrics is not None:metrics['enhanced']=context['metrics']
        _copy_render_status(edits, scaled, saved['ratio'])
        if load_image(enhanced).size != tuple(n*saved['ratio'] for n in load_image(source).size):
            raise ValueError('超分译图尺寸错误，旧结果保留')
        target = edition_path(library, book, index, 'translated')
        extra.append((target, enhanced))
    json_path = directory/'edits.json'
    write_json(json_path, edits)
    if extra:
        metadata = directory/'enhanced.json'
        write_json(metadata, {'dependencies':{'source':source_hash,'translated':fingerprint(normal),
                                             'edits':fingerprint(json_path)},
                              'options':saved,'output_sha256':fingerprint(enhanced)})
        extra.append((target.with_suffix('.json'), metadata))
    if cancel():
        raise InterruptedError('生成已暂停，旧译图保留')
    if fingerprint(source) != source_hash:
        raise ValueError('原图在处理期间发生变化，请重试')
    outputs=[(translated_path(library,book,index),Path(normal)),(edit_path(library,book,index),json_path)] + extra
    for kind in ('normal','enhanced'):
        context=render_context(library,book,index,kind,directory)
        if context['staged'].is_file():outputs.append((context['state'],context['staged']))
    from .colorization import stage_color_sync
    sources={'translated':(normal,None)}
    if extra:
        sources['upscaled_translated']=(enhanced,metadata)
    color_outputs,color_metrics=stage_color_sync(library,book,index,edits,directory,sources)
    outputs.extend(color_outputs)
    if metrics is not None:metrics['colored']=color_metrics
    return outputs


def commit_page_editions(outputs, directory):
    """Roll back the entire edition set if any file replacement fails."""
    backup = Path(directory)/'rollback'
    backup.mkdir()
    previous = []
    for n,(target,_) in enumerate(outputs):
        old = backup/str(n)
        existed = target.exists()
        if existed:
            shutil.copy2(target, old)
        previous.append((target,old,existed))
    try:
        for target, staged in outputs:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name+'.edition-tmp')
            shutil.copy2(staged, temporary)
            os.replace(temporary, target)
    except BaseException:
        for target, old, existed in previous:
            if existed:
                shutil.copy2(old, target)
            else:
                target.unlink(missing_ok=True)
        raise
    finally:
        for target,_ in outputs:
            target.with_name(target.name+'.edition-tmp').unlink(missing_ok=True)


def render_saved_page(library, book, index, edits, settings, *, cancel=lambda:False,
                      backend=None, renderer=render_page, preserve_upscale=True,profile_kind=None):
    from .edition_review import isolated,render_review
    if isolated(library,book,index):
        return render_review(library,book,index,'translated',edits,settings,cancel=cancel,backend=backend)
    if backend is None and any(r.get('processing_engine')=='mtu' for r in edits.get('regions',[])):
        from .render_pool import render_process
        with render_process(library,cancel,settings.gpu) as process:
            return render_saved_page(library,book,index,edits,settings,cancel=cancel,backend=process,
                                     renderer=renderer,preserve_upscale=preserve_upscale,profile_kind=profile_kind)
    from .pipeline import translated_path
    source = library.path(book)/library.manifest(book)['units'][index]['path']
    data = deepcopy(edits)
    metrics={'page':index+1};started=time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='page-editions-',dir=library.path(book)) as folder:
        directory = Path(folder)
        normal = directory/'normal.png'
        if cancel():
            raise InterruptedError('生成已暂停，旧译图保留')
        context=render_context(library,book,index,'normal',directory)
        renderer(source, data.get('regions',[]), normal, settings.font_path,
                 settings.comic_scope, settings.comic_layout, data.get('manual_selections'),
                 settings.comic_style, settings=settings, cleanup=data.get('residual_cleanup'),
                 incremental=context,**({'backend':backend} if backend is not None else {}))
        context['metrics']['seconds']=round(time.perf_counter()-started,3)
        metrics['normal']=context['metrics']
        outputs = stage_page_editions(library,book,index,data,settings,normal,directory,cancel=cancel,backend=backend,
                                     profile=(upscale_profile(library,book,index,profile_kind) if profile_kind else None) if preserve_upscale else {},metrics=metrics)
        commit_page_editions(outputs, directory)
    edits.clear(); edits.update(data)
    metrics['seconds']=round(time.perf_counter()-started,3)
    try:
        with (library.root/'render-performance.jsonl').open('a',encoding='utf-8') as log:
            log.write(json.dumps(metrics)+'\n')
    except OSError:pass
    return translated_path(library, book, index)
