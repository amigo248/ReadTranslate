"""Validated per-page geometry and last-render state; no duplicate page images."""
from copy import deepcopy
from pathlib import Path
import hashlib,json,itertools
import numpy as np
from PIL import Image
from .config import write_json
from .embedded_runtime import resolve_font
from .comic_regions import text_orientation

VERSION=1
STATUS=('render_overflow','render_warning','render_font_size','render_box','boundary_warning')


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def token(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def geometry_regions(regions):
    from .text_replace import has_translated_text
    keys=('box','layout_box','selection','mode','text','text_polygons','source_orientation','is_dialogue',
          'ocr_engine','ocr_confidence','source_font_size','processing_engine','mtu_mask','mtu_bubble','render_scale')
    # Editing Chinese also marks OCR as reviewed in the editor. That must not
    # discard 48px geometry; review flags affect only MOCR duplicate retention.
    return [{k:r.get(k) for k in keys}|{'translated':has_translated_text(r),
        'dedup_reviewed':bool(r.get('ocr_reviewed') or r.get('style_reviewed') or r.get('selection') in ('include','exclude'))
            if r.get('ocr_engine','').startswith('mtu-mocr') else False} for r in regions]


def geometry_key(source,regions,scope,cleanup,manual):
    from .mtu_bridge import owned_cache_file
    shaped=geometry_regions(regions)
    assets=[]
    for r in regions:
        for key in ('mtu_mask','mtu_bubble'):
            value=r.get(key)
            asset=owned_cache_file(value)
            if asset.is_file():assets.append((value,digest(asset)))
    patches=deepcopy(manual or [])
    for p in patches:
        for r in p.get('regions',[]):
            for k in list(r):
                if k.startswith('render_'):r.pop(k)
    return token([VERSION,digest(source),shaped,scope,cleanup,patches,assets])


def style_key(region,font,layout):
    selected=resolve_font(region.get('font_path') or font)
    stamp=Path(selected).stat() if Path(selected).is_file() else None
    return token([region.get('translation'),region.get('font_size'),selected,
                  [stamp.st_size,stamp.st_mtime_ns] if stamp else None,region.get('weight','auto'),
                  region.get('color','#111111'),text_orientation(region,layout)])


def load_state(context,key,regions,font,layout):
    """A cache hit requires the actual currently displayed image to match."""
    if not context:return None
    try:
        state=json.loads(Path(context['state']).read_text('utf-8'))
        if state['version']!=VERSION or state['geometry']!=key:return None
        if digest(context['previous'])!=state['output_sha256']:return None
        cache=Path(context['state']).parent.resolve()
        cleaned=Path(state['cleaned']).resolve()
        if not cleaned.is_relative_to(cache.parent) or digest(cleaned)!=state['cleaned_sha256']:return None
        with Image.open(context['previous']) as image:width,height=image.size
        ids=set()
        for plan in state['plans']:
            i=plan['id'];x,y,w,h=plan['safe_bounds']
            if not isinstance(i,int) or i in ids or not 0<=i<len(regions):return None
            if not all(isinstance(n,int) for n in (x,y,w,h)) or min(x,y)<0 or min(w,h)<1 or x+w>width or y+h>height:return None
            ids.add(i)
            safe=Path(plan['safe_file']).resolve()
            if not safe.is_relative_to(cache) or digest(safe)!=plan['safe_sha256']:return None
            with Image.open(safe) as image:
                if image.size!=(w,h):return None
        if len(state['styles'])!=len(regions) or len(state['status'])!=len(regions):return None
        if not all(isinstance(item,dict) for item in state['status']):return None
        state['dirty']=[i for i,r in enumerate(regions) if state['styles'][i]!=style_key(r,font,layout)]
        return state
    except (OSError,ValueError,KeyError,TypeError,IndexError):return None


def load_review_state(context,source,regions,scope,cleanup,manual,font,layout):
    """Recover a verified pre-editor cache; never trust review flags as geometry."""
    if not context:return None
    try:
        saved=json.loads(Path(context['state']).read_text('utf-8'))
        shapes=saved.get('geometry_regions')
        if shapes:
            if len(shapes)!=len(regions):return None
            current_shapes=geometry_regions(regions)
            # Including an already rendered dark frame may expand its contour.
            # Rebuild that geometry rather than treating selection as style.
            if any(shapes[p['id']].get('selection')!=current_shapes[p['id']].get('selection')
                   for p in saved['plans']):return None
            allowed={'translated','dedup_reviewed','selection'}
            if any(any(old.get(k)!=new.get(k) for k in set(old)|set(new) if k not in allowed)
                   for old,new in zip(shapes,current_shapes)):return None
            previous=[]
            for shape in shapes:
                r={k:deepcopy(v) for k,v in shape.items() if k not in ('translated','dedup_reviewed')}
                r['translation']='translated' if shape['translated'] else ''
                r['ocr_reviewed']=shape['dedup_reviewed'];previous.append(r)
            key=geometry_key(source,previous,scope,cleanup,manual)
            return load_state(context,key,regions,font,layout)
        # Existing installed caches predate input snapshots. Reconstruct only
        # the editor's review/selection flags, and require the exact old hash.
        active=saved.get('active')
        if not active or len(active)!=len(regions):return None
        previous=deepcopy(regions)
        dirty=[]
        for i,r in enumerate(previous):
            r['translation']='translated' if active[i] else '';r.pop('global_replacement',None)
            if saved['styles'][i]!=style_key(regions[i],font,layout):dirty.append(i)
        choices=[]
        for i in dirty:
            r=previous[i];variants=[deepcopy(r)]
            if r.get('ocr_engine','').startswith('mtu-mocr'):
                variant=deepcopy(r);variant['ocr_reviewed']=False;variant['style_reviewed']=False
                variants.append(variant)
                if r.get('selection')=='include':
                    for selection in ('auto','exclude'):
                        variant=deepcopy(variant);variant['selection']=selection;variants.append(variant)
            choices.append(variants)
        if not choices or any(type(v) is not bool for v in active):return None
        # Bound work on very large edits; full regeneration remains available.
        for variants in itertools.islice(itertools.product(*choices),64):
            candidate=deepcopy(previous)
            for i,r in zip(dirty,variants):candidate[i]=r
            if any(candidate[p['id']].get('selection')!=regions[p['id']].get('selection')
                   for p in saved['plans']):continue
            key=geometry_key(source,candidate,scope,cleanup,manual)
            state=load_state(context,key,regions,font,layout)
            if state:return state
    except (OSError,ValueError,KeyError,TypeError,IndexError):pass
    return None


def load_activation_state(context,source,regions,scope,cleanup,manual,font,layout):
    """Accept only an empty-to-translated transition; recheck geometry below."""
    from .text_replace import has_translated_text
    if not context:return None
    try:
        saved=json.loads(Path(context['state']).read_text('utf-8'))
        active=saved.get('active')
        if active is None:
            # Compatibility with existing installed page caches. A formerly
            # empty frame has the exact style hash of its empty translation.
            active=[]
            for i,r in enumerate(regions):
                empty=deepcopy(r);empty['translation']='';empty.pop('global_replacement',None)
                active.append(saved['styles'][i]!=style_key(empty,font,layout))
        if len(active)!=len(regions) or any(type(v) is not bool for v in active):return None
        current=[has_translated_text(r) for r in regions]
        if any(a and not b for a,b in zip(active,current)) or active==current:return None
        previous=deepcopy(regions)
        for r,enabled in zip(previous,active):
            r['translation']='translated' if enabled else '';r.pop('global_replacement',None)
        key=geometry_key(source,previous,scope,cleanup,manual)
        state=load_state(context,key,regions,font,layout)
        if state:state['activation_ids']=[i for i,(a,b) in enumerate(zip(active,current)) if not a and b]
        return state
    except (OSError,ValueError,KeyError,TypeError,IndexError):return None


def activation_plans_disjoint(state,plans,regions):
    """Reuse old geometry only when new masks cannot affect any other text."""
    for cached in state['plans']:
        x,y,w,h=cached['safe_bounds']
        with Image.open(cached['safe_file']) as image:crop=np.asarray(image.convert('L'))
        if any(np.any((p['safe'][y:y+h,x:x+w]>0)&(crop>0)) for p in plans):return False
    selected={p['id'] for p in plans}
    for i,r in enumerate(regions):
        if i in selected:continue
        x,y,w,h=map(int,r['box'])
        if any(np.any(p['safe'][max(0,y):max(0,y+h),max(0,x):max(0,x+w)]) for p in plans):return False
    return True


def removal_plans_disjoint(state,removed):
    """Original pixels may be restored only outside every retained safe mask."""
    pending=[p for p in state['plans'] if p['id'] in removed]
    retained=[p for p in state['plans'] if p['id'] not in removed]
    for old in pending:
        x,y,w,h=old['safe_bounds']
        with Image.open(old['safe_file']) as image:mask=np.asarray(image.convert('L'))>0
        for other in retained:
            a,b,c,d=other['safe_bounds']
            left,top,right,bottom=max(x,a),max(y,b),min(x+w,a+c),min(y+h,b+d)
            if right<=left or bottom<=top:continue
            with Image.open(other['safe_file']) as image:keep=np.asarray(image.convert('L'))>0
            if np.any(mask[top-y:bottom-y,left-x:right-x]&keep[top-b:bottom-b,left-a:right-a]):return False
    return True


def removal_only_transition(state,regions,removed):
    """A cached, retained frame becoming empty cannot change other ownership.

    Source/contour inputs and assets have already passed load_review_state.
    Only clearing retained frames (and confirming those same frames) qualifies;
    other selection or duplicate-retention changes still require planning.
    """
    previous=state.get('geometry_regions')
    if not removed or not previous:return False
    current=geometry_regions(regions)
    for i,(old,new) in enumerate(zip(previous,current)):
        if old.get('selection')!=new.get('selection'):return False
        if i in removed:
            if not old['translated'] or new['translated']:return False
            if old['dedup_reviewed'] and not new['dedup_reviewed']:return False
        elif (old['translated']!=new['translated'] or old['dedup_reviewed']!=new['dedup_reviewed']):return False
    return True


def cached_plans(state,regions,font,layout,size):
    plans=[]
    dirty=set(state['dirty'])
    for cached in state['plans']:
        i=cached['id']
        if i not in dirty:continue
        plan={k:deepcopy(v) for k,v in cached.items() if k not in ('safe_file','safe_sha256','safe_bounds')}
        x,y,w,h=cached['safe_bounds']
        with Image.open(cached['safe_file']) as image:crop=np.asarray(image.convert('L'))
        safe=np.zeros((size[1],size[0]),np.uint8);safe[y:y+h,x:x+w]=crop;plan['safe']=safe
        r=regions[i];plan['source']=r.get('text','');plan['translation']=r.get('translation','')
        plan['font']=resolve_font(r.get('font_path') or font)
        plan['size']=max(8,min(128*r.get('render_scale',1),int(r.get('font_size') or cached['reference_size'])))
        plan['direction']=text_orientation(r,layout)
        plan['weight']=cached['auto_weight'] if r.get('weight','auto')=='auto' else r['weight']
        from PIL import ImageColor
        plan['color']=[245,245,245] if cached['dark'] else list(ImageColor.getrgb(r.get('color','#111111')))
        plans.append(plan)
    return plans


def restore_status(state,regions):
    for r,saved in zip(regions,state['status']):
        for key in STATUS:
            r.pop(key,None)
            if key in saved:r[key]=deepcopy(saved[key])


def stage_state(context,key,regions,plans,cleaned,output,font,layout,image,previous=None,removed=()):
    if not context:return
    from .images import source_typography
    assets=Path(context['state']).parent/(Path(context['state']).stem+'-masks')
    assets.mkdir(parents=True,exist_ok=True)
    ids={p['id'] for p in plans}
    stored=[deepcopy(p) for p in (previous or {}).get('plans',[]) if p['id'] not in ids and p['id'] not in removed]
    for plan in plans:
        ys,xs=np.nonzero(plan['safe']);x,y=int(xs.min()),int(ys.min());w,h=int(xs.max()-x+1),int(ys.max()-y+1)
        path=assets/(str(plan['id'])+'.png');Image.fromarray(plan['safe'][y:y+h,x:x+w]).save(path)
        r=regions[plan['id']];reference,bold=source_typography(image,r)
        stored.append({k:deepcopy(v) for k,v in plan.items() if k!='safe'}|{
            'safe_file':str(path.resolve()),'safe_sha256':digest(path),'safe_bounds':[x,y,w,h],
            'reference_size':r.get('source_font_size') or reference,'auto_weight':'bold' if bold else 'regular',
            'dark':plan['background']==[0,0,0]})
    stored.sort(key=lambda p:p['id'])
    write_json(Path(context['staged']),{'version':VERSION,'geometry':key,'plans':stored,
        'geometry_regions':geometry_regions(regions),
        'active':[bool(r.get('translation') or r.get('global_replacement',{}).get('base')) for r in regions],
        'styles':[style_key(r,font,layout) for r in regions],
        'status':[{k:deepcopy(r[k]) for k in STATUS if k in r} for r in regions],
        'cleaned':str(Path(cleaned).resolve()),'cleaned_sha256':digest(cleaned),'output_sha256':digest(output)})


def stage_cached_state(context,state,regions,output,font,layout):
    state=deepcopy(state);state.pop('dirty',None)
    state['styles']=[style_key(r,font,layout) for r in regions]
    state['geometry_regions']=geometry_regions(regions)
    state['status']=[{k:deepcopy(r[k]) for k in STATUS if k in r} for r in regions]
    state['output_sha256']=digest(output)
    write_json(Path(context['staged']),state)
