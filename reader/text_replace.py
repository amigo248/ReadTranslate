"""Literal OCR/translation replacement with preview, staged rendering and rollback."""
import hashlib,json,os,re,shutil,tempfile,uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from contextlib import ExitStack
from .config import write_json
from .pipeline import edit_path,translated_path
from .images import render_page


def global_rules(settings):
    active=[];seen=set()
    for n,rule in enumerate(settings.text_replace_rules):
        if not isinstance(rule,dict):raise ValueError(f'第 {n+1} 行替换规则无效')
        query=rule.get('find','');replacement=rule.get('replace','')
        if not isinstance(query,str) or not isinstance(replacement,str):raise ValueError('替换规则必须是文字')
        if not query and not replacement:continue
        if not rule.get('enabled',True):continue
        if not query:raise ValueError(f'第 {n+1} 行规则的查找文字不能为空')
        key=query if settings.text_replace_case_sensitive else query.casefold()
        if key in seen:raise ValueError(f'第 {n+1} 行查找文字重复，请合并重复规则')
        seen.add(key);active.append((query,replacement))
    return sorted(active,key=lambda r:-len(r[0]))


def replace_global_text(text,settings):
    if not settings.text_replace_enabled:return text
    rules=global_rules(settings)
    if not rules:return text
    pattern=re.compile('|'.join(f'(?P<r{n}>{re.escape(query)})' for n,(query,_) in enumerate(rules)),
                       0 if settings.text_replace_case_sensitive else re.IGNORECASE)
    return pattern.sub(lambda match:rules[int(match.lastgroup[1:])][1],text)


def apply_global_replacements(data,settings):
    """Re-evaluate stored base text; treat a human-edited result as a new base."""
    def value(text,previous):
        base=previous['base'] if previous and text==previous.get('result') else text
        result=replace_global_text(base,settings)
        metadata={'base':base,'result':result} if previous or result!=base else None
        return result,metadata
    regions=list(data.get('regions',[]))
    regions.extend(r for s in data.get('manual_selections',[]) for r in s.get('regions',[]))
    for region in regions:
        if not region.get('translation') and not region.get('global_replacement'):continue
        region['translation'],meta=value(region.get('translation',''),region.get('global_replacement'))
        if meta:region['global_replacement']=meta
    if 'translations' in data:
        previous=data.get('global_replacement_paragraphs',{});metadata={}
        for n,text in enumerate(data['translations']):
            data['translations'][n],meta=value(text,previous.get(str(n)))
            if meta:metadata[str(n)]=meta
        if metadata:data['global_replacement_paragraphs']=metadata
    if data.get('title_translation') or data.get('global_replacement_title'):
        data['title_translation'],meta=value(data.get('title_translation',''),data.get('global_replacement_title'))
        if meta:data['global_replacement_title']=meta
    return data


def has_translated_text(region):
    return bool(region.get('translation') or region.get('global_replacement',{}).get('base'))


def suppressed_paragraphs(data):
    return {int(n) for n,meta in data.get('global_replacement_paragraphs',{}).items()
            if n.isdigit() and int(n)<len(data.get('translations',[])) and meta.get('base')
            and not meta.get('result','').strip() and data['translations'][int(n)]==meta.get('result')}


def file_hash(path):
    if not path.exists():return None
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def preview_replace(library,book_id,index,query,replacement,scope='page',field='translation',region_index=-1,case_sensitive=True,*,review_key=None):
    if not query:raise ValueError('请输入要查找的文字')
    if scope not in ('region','page','book') or field not in ('text','translation'):
        raise ValueError('替换范围或文本类型无效')
    manifest=library.manifest(book_id)
    if manifest['kind']!='comic':raise ValueError('该功能用于漫画 OCR 原文与译文')
    if not 0<=index<len(manifest['units']):raise ValueError('页面索引无效')
    pattern=re.compile(re.escape(query),0 if case_sensitive else re.IGNORECASE)
    pages=[];rows=[];total=0
    indexes=range(len(manifest['units'])) if scope=='book' else [index]
    for page in indexes:
        path=edit_path(library,book_id,page)
        if review_key:
            from .edition_review import ensure_page,data_path,valid_image
            ensure_page(library,book_id,page)
            if not valid_image(library,book_id,page,review_key):continue
            path=data_path(library,book_id,page,review_key)
        if not path.exists():continue
        original=json.loads(path.read_text('utf-8'));data=deepcopy(original)
        targets=[(f'文字框 {n+1}',r) for n,r in enumerate(data.get('regions',[]))]
        targets.extend((f'框选 {n+1} / 文字框 {m+1}',r) for n,s in enumerate(data.get('manual_selections',[])) for m,r in enumerate(s.get('regions',[])))
        if scope=='region':targets=[item for n,item in enumerate(targets) if n==region_index]
        changed=0
        for label,region in targets:
            old=region.get(field,'')
            new,count=pattern.subn(lambda _:replacement,old)
            if count and new!=old:
                region[field]=new;region['ocr_reviewed']=True
                region.pop('ocr_warning',None)
                rows.append({'page':page+1,'frame':label,'count':count,'before':old,'after':new})
                total+=count;changed+=1
        if changed:
            pages.append({'index':page,'data':data,'before_hash':file_hash(path),
                          'image_hash':file_hash(valid_image(library,book_id,page,review_key) if review_key else translated_path(library,book_id,page)),
                          'source_hash':file_hash(library.path(book_id)/manifest['units'][page]['path'])})
    return {'book_id':book_id,'pages':pages,'rows':rows,'matches':total,'query':query,
            'replacement':replacement,'scope':scope,'field':field,'case_sensitive':case_sensitive,'review_key':review_key}


def apply_replace(library,settings,plan,progress=lambda *_:None,cancel=lambda:False):
    if not plan['pages']:raise ValueError('没有可替换的文字，请先预览命中结果')
    book_id=plan['book_id'];root=library.path(book_id);manifest=library.manifest(book_id)
    pages=plan['pages'];total=len(pages);backend=None;backup=None;committing=False
    key=plan.get('review_key')
    def paths(index):
        if key:
            from .edition_review import data_path,valid_image
            return data_path(library,book_id,index,key),valid_image(library,book_id,index,key)
        return edit_path(library,book_id,index),translated_path(library,book_id,index)
    def verify():
        for page in pages:
            index=page['index']
            draft,image=paths(index)
            if (file_hash(draft)!=page['before_hash'] or not image or
                file_hash(image)!=page['image_hash'] or
                file_hash(root/manifest['units'][index]['path'])!=page['source_hash']):
                raise ValueError('页面在预览后发生变化，请重新预览；原校对和译图已保留')
    verify()
    try:
        with ExitStack() as leases,tempfile.TemporaryDirectory(prefix='text-replace-',dir=root) as folder:
            staged=Path(folder);outputs=[]
            for done,page in enumerate(pages):
                if cancel():raise InterruptedError('替换已取消，原校对和译图已保留')
                index=page['index'];edits=deepcopy(page['data'])
                progress(done,total,f'准备第 {index+1} 页替换后的译图；原文件尚未修改')
                if key:
                    from .edition_review import stage_review
                    staged_outputs,_,_=stage_review(library,book_id,index,key,edits,settings,staged/str(index),cancel=cancel)
                    outputs.extend(staged_outputs)
                    continue
                native=any(r.get('processing_engine')=='mtu' for r in edits.get('regions',[]))
                if native and backend is None:
                    from .render_pool import render_process
                    backend=leases.enter_context(render_process(library,cancel,settings.gpu))
                from .page_render import stage_page_editions,render_context
                directory=staged/str(index);directory.mkdir()
                context=render_context(library,book_id,index,'normal',directory)
                target=staged/f'{index:06d}.png'
                render_page(root/manifest['units'][index]['path'],edits.get('regions',[]),target,
                            settings.font_path,settings.comic_scope,settings.comic_layout,
                            edits.get('manual_selections'),settings.comic_style,
                            settings=settings,cleanup=edits.get('residual_cleanup'),incremental=context,
                            **({'backend':backend} if native else {}))
                outputs.extend(stage_page_editions(library,book_id,index,edits,settings,target,directory,
                                                   cancel=cancel,backend=backend))
            if cancel():raise InterruptedError('替换已取消，原校对和译图已保留')
            verify()
            backup=root/'backups'/('text-replace-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
            backup.mkdir(parents=True)
            saved=[]
            for destination,_ in outputs:
                relative=destination.relative_to(root)
                old=backup/relative
                if destination.exists():old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(destination,old)
                saved.append({'path':relative.as_posix(),'existed':destination.exists()})
            write_json(backup/'replacement.json',{'query':plan['query'],'replacement':plan['replacement'],
                'matches':plan['matches'],'field':plan['field'],'scope':plan['scope'],'files':saved})
            committing=True
            for destination,source in outputs:
                destination.parent.mkdir(parents=True,exist_ok=True)
                temporary=destination.with_name(destination.name+'.replace-tmp')
                shutil.copy2(source,temporary);os.replace(temporary,destination)
            progress(total,total,'文本替换完成，所有受影响译图已生成')
            return {'book_id':book_id,'indexes':[p['index'] for p in pages],'matches':plan['matches'],'backup':str(backup)}
    except Exception:
        if committing:
            for item in saved:
                destination=root/item['path']
                if item['existed']:shutil.copy2(backup/item['path'],destination)
                elif destination.exists():destination.unlink()
        raise
