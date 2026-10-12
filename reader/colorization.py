"""Image-only colorization; manual text review remains a separate workflow."""
from pathlib import Path
from copy import deepcopy
from dataclasses import fields
import hashlib,json,os,shutil,tempfile,time,uuid
import cv2
import numpy as np
from PIL import Image,ImageDraw
from .config import write_json,Settings
from .upscaling import fingerprint

EDITIONS=(('原版','original'),('中文版','translated'),('原版超分','upscaled_original'),('中文版超分','upscaled_translated'))
COLOR_FIELDS=tuple(f.name for f in fields(Settings) if f.name.startswith('color_'))


def original_path(library,book,index):
    return library.path(book)/library.manifest(book)['units'][index]['path']


def source_path(library,book,index,edition):
    from .pipeline import translated_path
    from .upscaling import available_edition
    if edition=='original':return original_path(library,book,index)
    if edition=='translated':
        path=translated_path(library,book,index)
        if path.is_file():return path
        raise ValueError(f'第 {index+1} 页尚未生成中文版，请先翻译或校对排版')
    if edition in ('upscaled_original','upscaled_translated'):
        path=available_edition(library,book,index,edition.removeprefix('upscaled_'))
        if path:return path
        raise ValueError(f'第 {index+1} 页没有有效的对应超分版，请先生成超分')
    raise ValueError('上色来源版本无效')


def profile_stamp(library,book,index,edition,metadata=None):
    if not edition.startswith('upscaled_'):return None
    from .upscaling import edition_path
    path=Path(metadata) if metadata else edition_path(library,book,index,edition.removeprefix('upscaled_')).with_suffix('.json')
    return json.loads(path.read_text('utf-8'))['options']


def pointer_path(library,book,index,edition):
    if edition not in {v for _,v in EDITIONS}:raise ValueError('上色版本无效')
    return library.path(book)/'colored'/edition/f'{index:06d}.json'


def read_record(library,book,index,edition):
    try:
        record=json.loads(pointer_path(library,book,index,edition).read_text('utf-8'))
        folder=(library.path(book)/'colored'/record['run']).resolve()
        root=(library.path(book)/'colored/runs').resolve()
        if not folder.is_relative_to(root) or not folder.is_dir():return None
        if record.get('edition')!=edition or record.get('page')!=index:return None
        return record,folder
    except (OSError,ValueError,TypeError,KeyError):return None


def available_color(library,book,index,edition):
    from .edition_review import record,valid_image
    if edition.endswith('translated') and record(library,book,index,'colored_'+edition):
        return valid_image(library,book,index,'colored_'+edition)
    saved=read_record(library,book,index,edition)
    if not saved:return None
    record,folder=saved
    try:
        from .edition_review import isolated
        source=folder/'source.png' if isolated(library,book,index) else source_path(library,book,index,edition)
        expected=record.get('snapshot_hash',record['source_hash']) if isolated(library,book,index) else record['source_hash']
        if record.get('stale') or expected!=fingerprint(source):return None
        if record['original_hash']!=fingerprint(original_path(library,book,index)):return None
        if not isolated(library,book,index) and record.get('profile')!=profile_stamp(library,book,index,edition):return None
        path=folder/'result.png'
        if record['output_hash']!=fingerprint(path):return None
        return path
    except (OSError,ValueError,KeyError):return None


def page_edits(library,book,index):
    from .pipeline import edit_path
    try:return json.loads(edit_path(library,book,index).read_text('utf-8'))
    except (OSError,ValueError):return {'regions':[]}


def protection_mask(image,edits,original_size,plan_states=(),cache_dir=None):
    """Use original-coordinate OCR, actual rendered bounds and verified safe masks."""
    width,height=image.size;sx=width/original_size[0];sy=height/original_size[1]
    mask=Image.new('L',image.size);draw=ImageDraw.Draw(mask)
    def box(value,offset=(0,0)):
        if not value or len(value)!=4:return
        x,y,w,h=value; x=(x+offset[0])*sx; y=(y+offset[1])*sy
        pad=max(3,round(4*max(sx,sy)))
        draw.rectangle((max(0,int(x)-pad),max(0,int(y)-pad),min(width-1,int(x+w*sx)+pad),min(height-1,int(y+h*sy)+pad)),fill=255)
    for r in edits.get('regions',[]):
        for key in ('box','layout_box','render_box'):box(r.get(key))
    for selection in edits.get('manual_selections',[]):
        box(selection.get('box'));box(selection.get('recognition_box'))
    result=np.asarray(mask).copy()
    for state_path in plan_states:
        try:
            state=json.loads(Path(state_path).read_text('utf-8'))
            for plan in state['plans']:
                p=Path(plan['safe_file'])
                allowed=Path(cache_dir).resolve() if cache_dir else Path(state_path).parent.resolve()
                if not p.resolve().is_relative_to(allowed):continue
                if fingerprint(p)!=plan['safe_sha256']:continue
                x,y,w,h=plan['safe_bounds']
                if x<0 or y<0 or x+w>width or y+h>height:continue
                with Image.open(p) as safe:
                    if safe.size!=(w,h):continue
                    result[y:y+h,x:x+w]|=np.asarray(safe.convert('L'))
        except (OSError,ValueError,KeyError,TypeError):continue
    return result>0


def state_paths(library,book,index,edition,directory=None):
    from .page_render import render_context
    kind='enhanced' if edition.startswith('upscaled_') else 'normal'
    context=render_context(library,book,index,kind,directory or library.root)
    paths=[context['state']]
    if directory and context['staged'].is_file():paths.insert(0,context['staged'])
    return paths


def changed_review_mask(image,changed,plan_states,cache_dir):
    """Manual review only: reuse trusted renderer masks for modified text frames.

    Pure colorizer output may differ from the monochrome image everywhere. Do
    not replace unchanged speech balloons when the user corrects a single frame.
    """
    result=np.zeros((image.height,image.width),dtype=bool)
    allowed_root=Path(cache_dir).resolve()
    for state_path in plan_states:
        try:
            state=json.loads(Path(state_path).read_text('utf-8'))
            for plan in state['plans']:
                path=Path(plan['safe_file']);x,y,w,h=plan['safe_bounds']
                if not path.resolve().is_relative_to(allowed_root) or fingerprint(path)!=plan['safe_sha256']:continue
                if x<0 or y<0 or x+w>image.width or y+h>image.height:continue
                with Image.open(path) as mask:
                    if mask.size!=(w,h):continue
                    mask=np.asarray(mask.convert('L'))>0
                if np.any(changed[y:y+h,x:x+w]&mask):result[y:y+h,x:x+w]|=mask
        except (OSError,ValueError,KeyError,TypeError):continue
    return result


def colorize_book(library,book,settings,indexes,edition,reference='',progress=lambda *_:None,
                  cancel=lambda:False,job_id=None,backend=None,force=False):
    from .color_backends import create_backend
    if library.manifest(book)['kind']!='comic':raise ValueError('上色仅用于漫画')
    indexes=list(dict.fromkeys(indexes))
    if not indexes or any(not isinstance(i,int) or not 0<=i<len(library.manifest(book)['units']) for i in indexes):raise ValueError('上色页码范围无效')
    # Validate every selected edition before doing any billable request.
    for index in indexes:source_path(library,book,index,edition)
    from .color_denoise import validate_sigma
    validate_sigma(settings.color_denoise_sigma)
    from .color_sizes import output_size,dimension_trace,trace_text
    requested=output_size(settings)
    root=library.path(book);saved_options={k:getattr(settings,k) for k in COLOR_FIELDS}
    refs=root/'colored/references';ref=None
    if reference:
        path=Path(reference)
        if not path.is_file():raise ValueError('参考图片不存在，请重新选择')
        with Image.open(path) as image:reference_image=image.convert('RGB').copy()
        refs.mkdir(parents=True,exist_ok=True)
        ref=refs/(fingerprint(path)+'.png')
        if not ref.exists():reference_image.save(ref)
    ref_hash=fingerprint(ref) if ref else ''
    names={'color_engine','color_max_side'}
    if settings.color_engine=='klein':names.update({'color_comfy_url','color_klein_model','color_klein_encoder','color_klein_vae','color_prompt'})
    elif settings.color_engine=='nano':names.update({'color_nano_url','color_nano_model'})
    elif settings.color_engine=='openai':names.update({'color_openai_url','color_openai_model','color_openai_quality'})
    else:raise ValueError('上色模型无效')
    recipe_options={k:v for k,v in saved_options.items() if k in names}
    # Auto preserves the pre-upgrade cache; explicit resolution is a new recipe.
    if requested!='auto':recipe_options['color_'+settings.color_engine+'_size']=requested
    else:recipe_options['resolution_policy']='follow-upload-v1'
    # Disabled keeps existing image-only cache valid; enabled strengths differ.
    if settings.color_denoise_sigma>=0:recipe_options['color_denoise_sigma']=settings.color_denoise_sigma
    if settings.color_engine in ('nano','openai'):
        recipe_options.update({k:saved_options[k] for k in ('color_square','color_prompt','color_history_pages')})
        if settings.color_engine=='openai':recipe_options['color_openai_interface']=settings.color_openai_interface
    recipe=hashlib.sha256(json.dumps(['colorize-only-v2',recipe_options,ref_hash,edition],sort_keys=True).encode()).hexdigest()
    job_id=job_id or library.create_job(book,'colorize',len(indexes),{'indexes':indexes,'edition':edition,
                           'options':saved_options,'reference':str(ref.relative_to(root)) if ref else '',
                           'reference_hash':ref_hash,'force':force,'completed':{}})
    job=next(j for j in library.jobs() if j['id']==job_id)
    payload=json.loads(job['payload']);completed=payload.get('completed',{});done=0
    library.update_job(job_id,job['done'],'running','正在上色')
    own=backend is None;provider=None;warnings=[];last_dimensions=''
    try:
        for index in indexes:
            if cancel():raise InterruptedError('上色已暂停，已完成页面保留，可继续任务')
            source=source_path(library,book,index,edition);expected=fingerprint(source)
            from .edition_review import isolated,load_data,record as review_record,capture_generated,render_review
            independent=isolated(library,book,index) and edition.endswith('translated')
            own_key='colored_'+edition
            own_record=review_record(library,book,index,own_key) if independent else None
            own_data=load_data(library,book,index,own_key) if own_record else None
            input_data=load_data(library,book,index,edition) if independent else None
            review_snapshot={};old_color_record=read_record(library,book,index,edition)
            if own_record:
                from .edition_review import folder as review_folder,data_path as review_data_path
                owned=review_folder(library,book,index,own_key)
                review_snapshot={p:p.read_bytes() for p in (review_data_path(library,book,index,own_key),owned/'record.json',owned/'rendered.json') if p.is_file()}
            history_paths=[]
            if settings.color_engine in ('nano','openai'):
                for previous in range(max(0,index-settings.color_history_pages),index):
                    path=available_color(library,book,previous,edition)
                    if path:history_paths.append(path)
            history_hashes=[fingerprint(path) for path in history_paths]
            page_recipe=hashlib.sha256(json.dumps([recipe,history_hashes]).encode()).hexdigest() if history_hashes else recipe
            existing=read_record(library,book,index,edition)
            current=available_color(library,book,index,edition)
            same=existing and current and existing[0].get('recipe')==page_recipe
            # Retain successful pages of an older paused job when migrating its
            # remaining pages to image-only processing; never charge twice.
            resumed=existing and current and completed.get(str(index))==existing[0]['output_hash']
            if resumed or same and not force:
                done+=1;library.update_job(job_id,done)
                last_dimensions=trace_text(existing[0].get('dimensions'))
                progress(done,len(indexes),f'第 {index+1} 页上色已完成，跳过重复请求');continue
            progress(done,len(indexes),f'第 {index+1} 页：仅图片上色')
            with Image.open(source) as image:image=image.convert('RGB').copy()
            if provider is None:
                if backend is None and (settings.color_engine=='klein' or settings.color_denoise_sigma>=0 and settings.gpu):
                    from .render_pool import close_render_pool
                    close_render_pool()
                provider=backend or create_backend(settings,library,book,cancel)
            reference_image=None
            if ref:
                with Image.open(ref) as loaded:reference_image=loaded.convert('RGB').copy()
            if settings.color_engine in ('nano','openai') and hasattr(provider,'history'):
                provider.history=[]
                for path in history_paths:
                    with Image.open(path) as loaded:provider.history.append(loaded.convert('RGB').copy())
            generated=provider.colorize(image,reference_image,seed=index)
            if hasattr(provider,'history'):provider.history=[]
            # Keep a completed cloud response even if pause arrived during its request.
            if fingerprint(source)!=expected:raise ValueError('上色期间来源图片发生变化，旧版本已保留，请重新发起任务')
            folder=root/'colored/runs'/uuid.uuid4().hex/f'{index:06d}'
            folder.mkdir(parents=True,exist_ok=True)
            # MTU's colorize-only branch publishes the colorizer output directly.
            # Only format/size normalization is needed; never OCR, mask, restore
            # source lettering/ink, transfer luminance, repair or render text.
            from .color_geometry import normalize_result,GEOMETRY_VERSION
            geometry=getattr(provider,'last_geometry',None)
            raw_model=getattr(provider,'last_raw_result',None)
            if raw_model is None:raw_model=generated
            raw_model.convert('RGB').save(folder/'model.png')
            if geometry and generated.size==image.size:result=generated.convert('RGB')
            else:result,geometry=normalize_result(generated,image.size,source=image)
            result.save(folder/'base.png');result.save(folder/'result.png')
            image.save(folder/'source.png')
            # An empty compatibility mask enables later user-initiated review
            # without detecting text or producing any mask during colorization.
            Image.new('L',image.size,0).save(folder/'protected.png')
            record={'version':2,'workflow':'colorize_only','run':folder.relative_to(root/'colored').as_posix(),'page':index,'edition':edition,
                    'engine':settings.color_engine,'options':saved_options,'recipe':page_recipe,'reference_hash':ref_hash,'history_hashes':history_hashes,
                    'created':time.time(),'source_hash':expected,'original_hash':fingerprint(original_path(library,book,index)),
                    'profile':profile_stamp(library,book,index,edition),'output_hash':fingerprint(folder/'result.png'),
                    'snapshot_hash':fingerprint(folder/'source.png'),'base_hash':fingerprint(folder/'base.png'),
                    'mask_hash':fingerprint(folder/'protected.png'),'stale':False,'refresh':'generated'}
            record.update(geometry=geometry,geometry_version=GEOMETRY_VERSION,model_file='model.png',model_hash=fingerprint(folder/'model.png'))
            record['model_raw_available']=True
            sizes=getattr(provider,'last_sizes',None) or dimension_trace(image.size,None,None,requested,raw_model.size,result.size)
            sizes=dict(sizes,saved=list(result.size))
            record['dimensions']=sizes
            last_dimensions=trace_text(sizes)
            # The pointer is the last write: incomplete runs never become visible.
            write_json(folder/'record.json',record);write_json(pointer_path(library,book,index,edition),record)
            if independent:
                try:
                    capture_generated(library,book,index,own_key,folder/'result.png',input_data,record.get('profile'))
                    if own_data is not None:
                        render_review(library,book,index,own_key,own_data,settings,cancel=lambda:False,
                                      force_regions=(own_record or {}).get('reviewed_regions',[]))
                        record=read_record(library,book,index,edition)[0]
                except BaseException:
                    for p,raw in review_snapshot.items():p.write_bytes(raw)
                    if old_color_record:write_json(pointer_path(library,book,index,edition),old_color_record[0])
                    raise
            completed[str(index)]=record['output_hash'];payload['completed']=completed
            with library.connect() as conn:conn.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),job_id))
            done+=1;library.update_job(job_id,done)
            message=f'第 {index+1} 页上色已保存（独立版本）'
            message+='\n'+last_dimensions
            if geometry.get('warning'):message+='；'+geometry['warning']
            if geometry.get('warning'):warnings.append(index+1)
            progress(done,len(indexes),message)
        message='上色完成；原版、中文版及超分版保留'
        if last_dimensions:message+='\n末页尺寸：'+last_dimensions
        if warnings:message+='；第 '+','.join(map(str,warnings))+' 页存在画布或边缘差异，已保存未拉伸的结果，请检查效果'
        library.update_job(job_id,done,'complete',message);return book
    except InterruptedError as error:
        library.update_job(job_id,done,'paused',str(error));raise
    except Exception as error:
        library.update_job(job_id,done,'failed',str(error));raise
    finally:
        if own and provider:provider.close()


def stage_color_sync(library,book,index,edits,directory,sources):
    """Only local compositing; called after existing incremental text rendering."""
    if not any(pointer_path(library,book,index,e).is_file() for _,e in EDITIONS):return [],[]
    outputs=[];stats=[];original=original_path(library,book,index)
    with Image.open(original) as image:original_size=image.size
    for _,edition in EDITIONS:
        if edition not in sources:continue
        saved=read_record(library,book,index,edition)
        derived=False;donor=None
        if not saved and edition.endswith('translated'):
            parent=edition.replace('translated','original')
            donor=read_record(library,book,index,parent) if available_color(library,book,index,parent) else None
            if donor:
                saved=donor;derived=True
        if not saved:continue
        record,folder=saved;source,meta=sources[edition];source=Path(source)
        fresh=deepcopy(record);prefix=Path(directory)/('color-'+edition);prefix.mkdir(exist_ok=True)
        destination=folder
        if derived:
            destination=library.path(book)/'colored/runs'/uuid.uuid4().hex/f'{index:06d}'
            fresh.update(edition=edition,run=destination.relative_to(library.path(book)/'colored').as_posix(),
                         profile=profile_stamp(library,book,index,edition,meta),derived_from=record['run'])
        try:
            if record['original_hash']!=fingerprint(original) or (not derived and record.get('profile')!=profile_stamp(library,book,index,edition,meta)):
                raise ValueError('来源原画或超分方案已改变')
            for name,key in [('result.png','output_hash'),('source.png','snapshot_hash'),('protected.png','mask_hash'),('base.png','base_hash')]:
                if fingerprint(folder/name)!=record[key]:raise ValueError('上色缓存校验失败')
            if not derived and record['source_hash']==fingerprint(source):continue
            with Image.open(source) as image:current=image.convert('RGB').copy()
            with Image.open(folder/'source.png') as image:previous=image.convert('RGB').copy()
            if previous.size!=current.size:raise ValueError('来源尺寸已改变')
            with Image.open(folder/'result.png') as image:pixels=np.asarray(image.convert('RGB')).copy()
            with Image.open(folder/'protected.png') as image:old_mask=np.asarray(image)>0
            old=np.asarray(previous);new=np.asarray(current)
            changed=np.any(old!=new,axis=2)
            states=state_paths(library,book,index,edition,directory);cache=library.path(book)/'cache/page-state'
            if record.get('workflow')=='colorize_only':mask=old_mask|changed_review_mask(current,changed,states,cache)
            else:mask=old_mask|protection_mask(current,edits,original_size,states,cache)
            # Manual background cleanups are also bounded by the trusted source delta.
            # Refresh changed source pixels only, leaving all other colored pixels intact.
            if edition.endswith('translated'):
                # A translated edition's changes are produced by the reader's existing
                # mask-validated renderer, not an image generator.
                pixels[mask]=new[mask]
                outside=changed&~mask
                if outside.any():
                    colored_lab=cv2.cvtColor(pixels,cv2.COLOR_RGB2LAB)
                    current_lab=cv2.cvtColor(new,cv2.COLOR_RGB2LAB)
                    colored_lab[:,:,0]=current_lab[:,:,0]
                    updated=cv2.cvtColor(colored_lab,cv2.COLOR_LAB2RGB)
                    pixels[outside]=updated[outside]
                    ink=outside&(np.max(new,axis=2)<40);pixels[ink]=new[ink]
            else:pixels[changed]=new[changed]
            Image.fromarray(pixels).save(prefix/'result.png');current.save(prefix/'source.png')
            Image.fromarray(mask.astype('uint8')*255).save(prefix/'protected.png')
            fresh.update(source_hash=fingerprint(source),output_hash=fingerprint(prefix/'result.png'),
                         snapshot_hash=fingerprint(prefix/'source.png'),mask_hash=fingerprint(prefix/'protected.png'),
                         updated=time.time(),stale=False,refresh='local-text',changed_pixels=int(changed.sum()))
            if derived:
                model_name=record.get('model_file')
                if model_name in ('model.png','previous-base.png') and (folder/model_name).is_file():
                    if fingerprint(folder/model_name)!=record.get('model_hash'):raise ValueError('模型原始返回图校验失败')
                    shutil.copy2(folder/model_name,prefix/model_name);outputs.append((destination/model_name,prefix/model_name))
                shutil.copy2(folder/'base.png',prefix/'base.png');outputs.append((destination/'base.png',prefix/'base.png'))
            outputs.extend((destination/name,prefix/name) for name in ('result.png','source.png','protected.png'))
            stats.append({'edition':edition,'changed_pixels':int(changed.sum()),'model_requests':0})
        except (OSError,ValueError,KeyError,TypeError) as error:
            if derived:continue
            fresh.update(stale=True,reason=str(error));stats.append({'edition':edition,'stale':True,'model_requests':0})
        write_json(prefix/'record.json',fresh)
        outputs.append((destination/'record.json',prefix/'record.json'))
        outputs.append((pointer_path(library,book,index,edition),prefix/'record.json'))
    return outputs,stats


def sync_existing_colors(library,book,index,edits):
    """Repair stale text overlays on a fully translated page without network work."""
    from .edition_review import isolated
    if isolated(library,book,index):return
    from .page_render import commit_page_editions
    if not any(pointer_path(library,book,index,e).is_file() for _,e in EDITIONS):return
    sources={}
    for _,edition in EDITIONS:
        try:sources[edition]=(source_path(library,book,index,edition),None)
        except ValueError:pass
    with tempfile.TemporaryDirectory(prefix='color-refresh-',dir=library.path(book)) as folder:
        outputs,_=stage_color_sync(library,book,index,edits,Path(folder),sources)
        if outputs:commit_page_editions(outputs,Path(folder))
