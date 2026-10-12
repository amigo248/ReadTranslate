"""Independent Chinese editions: drafts, rendered text, caches and provenance.

Coordinates are always original-page coordinates. Readers never infer a child's
validity from a mutable sibling. Existing pixels are frozen before the first edit.
"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import hashlib,json,shutil,tempfile,time,uuid
import cv2
import numpy as np
from PIL import Image
from .config import write_json

KEYS=('translated','upscaled_translated','colored_translated','colored_upscaled_translated')
LABELS=dict(zip(KEYS,('中文版','中文版超分','彩色中文版','彩色中文版超分')))


def review_key(edition,colored=False):
    if edition not in ('translated','upscaled_translated'):raise ValueError('原版不提供校对，请切换到中文版')
    return ('colored_' if colored else '')+edition


def folder(library,book,index,key):
    if key not in KEYS:raise ValueError('校对版本无效')
    return library.path(book)/'reviews'/key/f'{index:06d}'


def marker(library,book,index):return library.path(book)/'reviews/pages'/f'{index:06d}.json'
def isolated(library,book,index):return marker(library,book,index).is_file()


def original_path(library,book,index):return library.path(book)/library.manifest(book)['units'][index]['path']


def data_path(library,book,index,key):
    if key=='translated':
        from .pipeline import edit_path
        return edit_path(library,book,index)
    return folder(library,book,index,key)/'edits.json'


def _read(path,default=None):
    try:return json.loads(Path(path).read_text('utf-8'))
    except (OSError,ValueError):return deepcopy(default)


def _hash(path):
    from .upscaling import fingerprint
    return fingerprint(Path(path)) if Path(path).is_file() else None


def _owned(library,book,value):
    root=library.path(book).resolve();path=(root/value).resolve()
    if not path.is_relative_to(root):raise ValueError('版本缓存路径无效')
    return path


def record(library,book,index,key):return _read(folder(library,book,index,key)/'record.json')


def valid_image(library,book,index,key):
    saved=record(library,book,index,key)
    if not saved:return None
    try:
        path=_owned(library,book,saved['image'])
        if saved['original_hash']!=_hash(original_path(library,book,index)) or _hash(path)!=saved['output_hash']:return None
        return path
    except (OSError,ValueError,KeyError,TypeError):return None


def _record(library,book,index,key,image,data,recipe=None,*,target=None):
    root=library.path(book)
    return {'version':1,'key':key,'page':index,'original_hash':_hash(original_path(library,book,index)),
            'image':Path(target or image).relative_to(root).as_posix(),'output_hash':_hash(image),
            'recipe':deepcopy(recipe),'updated':time.time(),'revision':uuid.uuid4().hex}


def load_data(library,book,index,key):
    own=_read(data_path(library,book,index,key))
    if own is not None:return own
    parent='upscaled_translated' if key=='colored_upscaled_translated' else 'translated'
    if key==parent:
        from .pipeline import edit_path
        return _read(edit_path(library,book,index),{'regions':[]})
    return deepcopy(load_data(library,book,index,parent))


def rendered_data(library,book,index,key):
    return _read(folder(library,book,index,key)/'rendered.json',load_data(library,book,index,key))


def context(library,book,index,key,directory):
    path=valid_image(library,book,index,key)
    return {'state':library.path(book)/'cache/page-state'/f'{index:06d}-review-{key}.json',
            'staged':Path(directory)/'state.json','previous':path,'metrics':{}}


def ensure_page(library,book,index):
    """Lazy migration changes no image pixels and freezes every existing sibling."""
    if isolated(library,book,index):return
    from .pipeline import edit_path,translated_path
    from .upscaling import edition_path
    from .colorization import read_record
    data=_read(edit_path(library,book,index),{'regions':[]})
    entries=[];normal=translated_path(library,book,index)
    if normal.is_file():entries.append(('translated',normal,data,None))
    enhanced=edition_path(library,book,index,'translated');meta=_read(enhanced.with_suffix('.json'))
    if meta and enhanced.is_file() and meta.get('dependencies',{}).get('source')==_hash(original_path(library,book,index)) and meta.get('output_sha256')==_hash(enhanced):
        entries.append(('upscaled_translated',enhanced,data,meta['options']))
    for edition in ('translated','upscaled_translated'):
        saved=read_record(library,book,index,edition)
        if not saved:continue
        info,run=saved;image=run/'result.png'
        if image.is_file() and info.get('original_hash')==_hash(original_path(library,book,index)) and info.get('output_hash')==_hash(image) and not info.get('stale'):
            entries.append(('colored_'+edition,image,data,info.get('profile')))
    for key,image,edits,recipe in entries:
        if record(library,book,index,key):continue
        destination=folder(library,book,index,key);destination.mkdir(parents=True,exist_ok=True)
        if key!='translated':write_json(data_path(library,book,index,key),deepcopy(edits))
        write_json(destination/'rendered.json',deepcopy(edits))
        write_json(destination/'record.json',_record(library,book,index,key,image,edits,recipe))
        if not key.startswith('colored_'):
            old=library.path(book)/'cache/page-state'/f'{index:06d}-{"enhanced" if key.startswith("upscaled_") else "normal"}.json'
            new=context(library,book,index,key,destination)['state']
            if old.is_file() and not new.exists():shutil.copy2(old,new)
    write_json(marker(library,book,index),{'version':1,'migrated':time.time()})


def capture_generated(library,book,index,key,image,data,recipe=None):
    """Only the generated edition gets a new record; siblings retain their snapshots."""
    destination=folder(library,book,index,key);destination.mkdir(parents=True,exist_ok=True)
    if key!='translated':write_json(data_path(library,book,index,key),deepcopy(data))
    write_json(destination/'rendered.json',deepcopy(data))
    write_json(destination/'record.json',_record(library,book,index,key,image,data,recipe))


def _flat(data):
    result=deepcopy(data.get('regions',[]))
    for patch in data.get('manual_selections',[]):
        ox,oy=patch['box'][:2]
        for region in patch.get('regions',[]):
            r=deepcopy(region)
            for key in list(r):
                if (key=='box' or key.endswith('_box')) and isinstance(r[key],list) and len(r[key])==4:r[key][:2]=[r[key][0]+ox,r[key][1]+oy]
                elif key=='text_polygons':r[key]=[[[x+ox,y+oy] for x,y in p] for p in r[key]]
            result.append(r)
    return result


def signature(region):
    keys=('box','layout_box','text','translation','font_size','font_path','source_font_size','weight','color','layout','mode','selection','text_polygons','ocr_reviewed','style_reviewed','global_replacement')
    defaults={'text':'','translation':'','font_size':0,'font_path':'','weight':'auto','layout':'auto','mode':'inpaint','selection':'auto','ocr_reviewed':False,'style_reviewed':False}
    return json.dumps({k:region.get(k,defaults.get(k)) for k in keys},sort_keys=True,ensure_ascii=False)


def draft_signature(data):return json.dumps([list(map(signature,_flat(data))),data.get('residual_cleanup')],sort_keys=True,ensure_ascii=False)


def _dirty(old,new):
    # Index changes (add/remove/reorder) deliberately expand the affected set.
    return {i for i in range(max(len(old),len(new))) if i>=len(old) or i>=len(new) or signature(old[i])!=signature(new[i])}


def _reference(library,book,index,recipe,size):
    from .images import load_image
    from .upscaling import ocr_image_path,available_edition
    original=original_path(library,book,index)
    if recipe:
        from .config import Settings
        settings=replace(Settings(),**{'upscale_'+k:v for k,v in dict(recipe,mode='before').items()})
        path=ocr_image_path(library,original,settings)
        if path.is_file() and load_image(path).size==size:return path,load_image(path)
        owned=available_edition(library,book,index,'original',dict(recipe,mode='only'))
        if owned and load_image(owned).size==size:return owned,load_image(owned)
    image=load_image(original)
    return original,image if image.size==size else image.resize(size,Image.Resampling.LANCZOS)


def _plan_mask(reference,old,new,dirty,font,scope,layout,cleanup):
    from .mtu_bridge import build_plans
    mask=np.zeros((reference.height,reference.width),np.uint8)
    plans=[]
    for regions,is_new in ((old,False),(new,True)):
        ids={i for i in dirty if i<len(regions)}
        if not ids:continue
        _,built=build_plans(reference,regions,scope,layout,font,cleanup,indexes=ids)
        for p in built:mask|=p['safe']
        if is_new:plans=built
        for i in ids:
            r=regions[i]
            for value in (r.get('box'),r.get('layout_box'),r.get('render_box')):
                if not value:continue
                from .comic_regions import cached_boundaries
                bounds=cached_boundaries(reference,[q['box'] for q in regions])
                x,y,w,h=map(int,value);x=max(0,x);y=max(0,y);w=min(w,reference.width-x);h=min(h,reference.height-y)
                if w>0 and h>0:
                    allowed,_=bounds.allowed([x,y,w,h]);mask[y:y+h,x:x+w]|=allowed.astype(np.uint8)*255
    return mask,plans


def _colored_render(library,book,index,key,old,new,current,reference,recipe,directory,settings,backend,cleanup=None,old_cleanup=None,clean_only=False,force_regions=()):
    """Clean/retype changed frames on the color bitmap, never paste monochrome fill."""
    from .colorization import read_record
    from .embedded_runtime import resolve_font
    from .cleanup import background_color,repair_background
    from .comic_regions import PageBoundaries
    dirty=_dirty(old,new)|set(force_regions)
    if cleanup!=old_cleanup:dirty.update(range(max(len(old),len(new))))
    if clean_only:dirty=set(range(max(len(old),len(new))))
    mask,plans=_plan_mask(reference,old,new,dirty,settings.font_path,settings.comic_scope,settings.comic_layout,None)
    # Do not erase an old frame when the replacement does not fit.
    for i in dirty:
        if i<len(new) and new[i].get('render_overflow'):
            from .mtu_bridge import build_plans
            _,kept=build_plans(reference,old,settings.comic_scope,settings.comic_layout,settings.font_path,indexes={i} if i<len(old) else set())
            for p in kept:mask[p['safe']>0]=0
            x,y,w,h=map(int,new[i].get('layout_box') or new[i]['box']);mask[max(0,y):y+h,max(0,x):x+w]=0
    from .cleanup import brush_mask
    if cleanup!=old_cleanup:
        bounds=PageBoundaries(reference)
        for value in (cleanup,old_cleanup):
            if value:mask|=brush_mask(reference,value,bounds)
    pixels=np.asarray(current).copy()
    saved=read_record(library,book,index,key.removeprefix('colored_'))
    if not saved:raise ValueError('对应彩色版本未生成，请先上色')
    info,run=saved
    base_path=run/'base.png'
    if _hash(base_path)!=info['base_hash']:raise ValueError('彩色底图缓存已改变，旧结果保留')
    from .images import load_image
    base=load_image(base_path)
    if base.size!=current.size:raise ValueError('彩色底图尺寸错误，旧结果保留')
    cache=library.path(book)/'cache/review-color'/key;cache.mkdir(parents=True,exist_ok=True)
    cache_key=hashlib.sha256((_hash(base_path)+json.dumps([old[i].get('render_box') for i in sorted(dirty) if i<len(old)])).encode()+mask.tobytes()).hexdigest()
    cleaned=cache/(cache_key+'.png')
    if mask.any() and not cleaned.exists():
        gray=cv2.cvtColor(np.asarray(base),cv2.COLOR_RGB2GRAY)
        count,labels=cv2.connectedComponents((mask>0).astype(np.uint8))
        repaired=np.asarray(base).copy();ink=np.zeros_like(mask)
        for label in range(1,count):
            component=labels==label;tone=np.median(gray[component])
            strokes=((gray>min(245,tone+40)) if tone<110 else (gray<max(25,tone-40))).astype(np.uint8)*255
            strokes=cv2.dilate(strokes,np.ones((3,3),np.uint8))&component.astype(np.uint8)*255
            fill=background_color(base,strokes) if strokes.any() else None
            if fill is not None:repaired[component]=fill
            else:ink|=strokes
        if ink.any():
            target=Path(directory)/'erase.png';Image.fromarray(ink).save(target)
            backend.request('clean',path=str(base_path),mask=str(target),output=str(cleaned),gpu=settings.gpu,fills=[],local=True)
            fresh=np.asarray(load_image(cleaned));repaired[ink>0]=fresh[ink>0]
        Image.fromarray(repaired).save(cleaned)
    if mask.any():pixels[mask>0]=np.asarray(load_image(cleaned))[mask>0]
    if cleanup:
        pixels=np.asarray(repair_background(current,Image.fromarray(pixels),cleanup,PageBoundaries(reference),backend=backend,gpu=settings.gpu))
    cleaned_frame=Path(directory)/'color-clean.png';Image.fromarray(pixels).save(cleaned_frame)
    output=Path(directory)/'image.png'
    payload=[]
    if not clean_only:
        for p in plans:
            safe=p['safe']&(mask>0).astype(np.uint8)*255
            if not safe.any():continue
            path=Path(directory)/f"safe-{p['id']}.png";Image.fromarray(safe).save(path)
            payload.append({k:v for k,v in p.items() if k!='safe'}|{'safe_mask':str(path)})
    if payload:
        response=backend.request('render',path=str(cleaned_frame),output=str(output),font=resolve_font(settings.font_path),plans=payload)
        for item in response.get('rendered',[]):
            if item['id']<len(new):new[item['id']].update(render_font_size=item['font_size'],render_box=item['box'])
        from .layout_status import mark_overflow
        for item in response.get('overflow',[]):
            i=item['id'];mark_overflow(new[i],item.get('message','译文放不下，请修改字体、字号或译文'))
            prior=np.asarray(current)
            for p in plans:
                if p['id']==i:
                    reverted=np.asarray(load_image(output)).copy();reverted[p['safe']>0]=prior[p['safe']>0];Image.fromarray(reverted).save(output)
    else:shutil.copy2(cleaned_frame,output)
    # The worker cannot modify any unrelated pixel, even if it returns a full page.
    result=np.asarray(load_image(output)).copy();result[mask==0]=np.asarray(current)[mask==0]
    Image.fromarray(result).save(output)
    return output,{'rendered_frames':len(payload),'changed_frames':len(dirty),'color_model_requests':0,'siblings_rendered':0}


def stage_review(library,book,index,key,data,settings,directory,*,cancel=lambda:False,backend=None,recipe=None,regenerate=False,clean_only=False,force_regions=()):
    ensure_page(library,book,index)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    from .images import load_image,render_page
    from .pipeline import translated_path
    from .upscaling import edition_path,scale_edits
    saved=record(library,book,index,key);previous=valid_image(library,book,index,key)
    colored=key.startswith('colored_');enhanced='upscaled_' in key
    recipe=recipe or (saved or {}).get('recipe')
    if enhanced and not recipe:raise ValueError('当前版本缺少超分参数，请先生成对应超分版')
    factor=recipe['ratio'] if enhanced else 1
    original=load_image(original_path(library,book,index));size=tuple(n*factor for n in original.size)
    if previous and load_image(previous).size!=size and not regenerate:raise ValueError('当前版本尺寸与超分参数不一致，请重新生成')
    data=deepcopy(data)
    from .text_replace import apply_global_replacements
    apply_global_replacements(data,settings)
    old=rendered_data(library,book,index,key)
    before=_hash(previous) if previous else None;original_hash=_hash(original_path(library,book,index))
    draft_hash=_hash(data_path(library,book,index,key))
    if cancel():raise InterruptedError('校对生成已暂停，旧版本保留')
    ctx=context(library,book,index,key,directory)
    reference_path,reference=_reference(library,book,index,recipe,size)
    effective=scale_edits(data,factor) if factor!=1 else deepcopy(data)
    if colored:
        if not previous:raise ValueError('当前彩色版本未生成，请先上色')
        current=load_image(previous)
        scaled_old=scale_edits(old,factor) if factor!=1 else old
        old_flat,new_flat=_flat(scaled_old),_flat(effective)
        color_dirty=_dirty(old_flat,new_flat)|set(force_regions)
        no_change=not color_dirty and effective.get('residual_cleanup')==scaled_old.get('residual_cleanup') and not clean_only
        if backend is None and not no_change:
            from .render_pool import render_process
            with render_process(library,cancel,settings.gpu) as process:
                return stage_review(library,book,index,key,data,settings,directory,cancel=cancel,backend=process,recipe=recipe,regenerate=regenerate,clean_only=clean_only,force_regions=force_regions)
        if no_change:
            output=directory/'image.png';shutil.copy2(previous,output)
            metrics={'changed_frames':0,'rendered_frames':0,'color_model_requests':0,'siblings_rendered':0}
        else:
            output,metrics=_colored_render(library,book,index,key,old_flat,new_flat,current,reference,recipe,directory,settings,backend,
                                           effective.get('residual_cleanup'),scaled_old.get('residual_cleanup'),clean_only,force_regions)
        # Flattening is only for plans; persist edits in their original containers.
        copies=[(r,(0,0)) for r in effective.get('regions',[])] + [(r,p['box'][:2]) for p in effective.get('manual_selections',[]) for r in p.get('regions',[])]
        for (left,offset),right in zip(copies,new_flat):
            for field in ('render_overflow','render_warning','render_font_size','render_box'):
                left.pop(field,None)
                if field in right:
                    left[field]=deepcopy(right[field])
                    if field=='render_box':left[field][:2]=[left[field][0]-offset[0],left[field][1]-offset[1]]
    else:
        if reference.size!=load_image(reference_path).size:
            reference_path=directory/'reference.png';reference.save(reference_path)
        native=any(r.get('processing_engine')=='mtu' for r in effective.get('regions',[]))
        if native and backend is None:
            from .render_pool import render_process
            with render_process(library,cancel,settings.gpu) as process:
                return stage_review(library,book,index,key,data,settings,directory,cancel=cancel,backend=process,recipe=recipe,regenerate=regenerate,clean_only=clean_only,force_regions=force_regions)
        output=directory/'image.png'
        render_page(reference_path,effective.get('regions',[]),output,settings.font_path,settings.comic_scope,settings.comic_layout,
                    effective.get('manual_selections'),settings.comic_style,settings=settings,cleanup=effective.get('residual_cleanup'),
                    backend=backend,incremental=ctx,clean_only=clean_only)
        metrics=ctx['metrics']|{'siblings_rendered':0}
        if previous and not regenerate and not clean_only:
            old_scaled=scale_edits(old,factor) if factor!=1 else old
            old_flat,new_flat=_flat(old_scaled),_flat(effective);dirty=_dirty(old_flat,new_flat)
            if old.get('residual_cleanup')!=data.get('residual_cleanup'):dirty.update(range(max(len(old_flat),len(new_flat))))
            mask,_=_plan_mask(reference,old_flat,new_flat,dirty,settings.font_path,settings.comic_scope,settings.comic_layout,effective.get('residual_cleanup'))
            if old.get('residual_cleanup')!=data.get('residual_cleanup'):
                from .cleanup import brush_mask
                from .comic_regions import PageBoundaries
                bounds=PageBoundaries(reference)
                mask|=brush_mask(reference,effective.get('residual_cleanup'),bounds)
                prior=old_scaled.get('residual_cleanup')
                if prior:mask|=brush_mask(reference,prior,bounds)
            pixels=np.asarray(load_image(output)).copy();pixels[mask==0]=np.asarray(load_image(previous))[mask==0]
            Image.fromarray(pixels).save(output)
            if ctx['staged'].is_file():
                state=_read(ctx['staged']);state['output_sha256']=_hash(output);write_json(ctx['staged'],state)
    from .page_render import _copy_render_status
    if factor!=1:_copy_render_status(data,effective,factor)
    else:data=effective
    if cancel():raise InterruptedError('校对生成已暂停，旧版本保留')
    if _hash(original_path(library,book,index))!=original_hash or (previous and _hash(previous)!=before):raise ValueError('当前版本在校对期间发生变化，请重新打开校对窗口')
    if _hash(data_path(library,book,index,key))!=draft_hash:raise ValueError('当前版本的校对草稿已改变，请重新打开校对窗口')
    target=previous
    if not target:
        target=edition_path(library,book,index,'translated') if enhanced else translated_path(library,book,index)
    staged_data=directory/'edits.json';write_json(staged_data,data)
    staged_record=directory/'record.json';fresh=_record(library,book,index,key,output,data,recipe,target=target)
    fresh['image']=target.relative_to(library.path(book)).as_posix();fresh['reviewed']=True
    fresh['reviewed_regions']=sorted(set((saved or {}).get('reviewed_regions',[]))|_dirty(_flat(old),_flat(data))|set(force_regions))
    write_json(staged_record,fresh)
    rendered=directory/'rendered.json';write_json(rendered,data)
    outputs=[(target,output),(data_path(library,book,index,key),staged_data),
             (folder(library,book,index,key)/'record.json',staged_record),(folder(library,book,index,key)/'rendered.json',rendered)]
    if ctx['staged'].is_file():outputs.append((ctx['state'],ctx['staged']))
    if enhanced and not colored:
        meta=directory/'enhanced.json'
        write_json(meta,{'dependencies':{'source':original_hash},'options':recipe,'output_sha256':_hash(output),'independent_review':True})
        outputs.append((target.with_suffix('.json'),meta))
    if colored:
        from .colorization import read_record,pointer_path
        info,run=read_record(library,book,index,key.removeprefix('colored_'));info=deepcopy(info)
        info.update(output_hash=_hash(output),refresh='independent-review',independent_review=True,updated=time.time(),stale=False)
        color_record=directory/'color-record.json';write_json(color_record,info)
        outputs.extend([(run/'record.json',color_record),(pointer_path(library,book,index,key.removeprefix('colored_')),color_record)])
    return outputs,data,metrics


def render_review(library,book,index,key,data,settings,*,cancel=lambda:False,backend=None,recipe=None,regenerate=False,force_regions=()):
    ensure_page(library,book,index)
    started=time.perf_counter();root=library.path(book)
    with tempfile.TemporaryDirectory(prefix='edition-review-',dir=root) as directory:
        outputs,updated,metrics=stage_review(library,book,index,key,data,settings,directory,cancel=cancel,backend=backend,recipe=recipe,regenerate=regenerate,force_regions=force_regions)
        backup=root/'backups'/('review-'+uuid.uuid4().hex[:8]);entries=[]
        for n,(target,staged) in enumerate(outputs):
            if target.is_file() and _hash(target)!=_hash(staged):
                copy=backup/(str(n)+target.suffix);copy.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,copy)
                entries.append({'path':target.relative_to(root).as_posix(),'backup':copy.name})
        if entries:write_json(backup/'files.json',{'edition':key,'page':index,'files':entries})
        from .page_render import commit_page_editions
        commit_page_editions(outputs,directory)
    data.clear();data.update(updated)
    metrics.update(page=index+1,edition=key,seconds=round(time.perf_counter()-started,3),siblings_rendered=0)
    with (library.root/'render-performance.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(metrics)+'\n')
    return valid_image(library,book,index,key)
