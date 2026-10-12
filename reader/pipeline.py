from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import hashlib
import tempfile
import math
from copy import deepcopy
from pathlib import Path

from .config import Settings, app_root, write_json
from .images import render_page, render_image, load_image
from .comic_regions import annotate_regions, region_enabled, PageBoundaries, needs_regrouping, annotate_boundaries, white_frame_boxes, prioritize_white_regions
from .library import Library
from .translation import CloudTranslator,create_translator


def edit_path(library: Library, book_id: str, index: int):
    return library.path(book_id) / "edits" / f"{index:06d}.json"


def translated_path(library: Library, book_id: str, index: int):
    return library.path(book_id) / "translated" / f"{index:06d}.png"


def selection_box(box, size):
    if len(box)!=4 or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in box):
        raise ValueError("框选坐标无效")
    x,y,w,h=[round(v) for v in box]
    left,top=max(0,x),max(0,y)
    right,bottom=min(size[0],x+w),min(size[1],y+h)
    if right-left<6 or bottom-top<6:
        raise ValueError("请框选至少 6×6 像素的有效区域")
    return [left,top,right-left,bottom-top]


def recognize_selection(library,book_id,index,box,settings,progress=lambda *_:None,cancel=lambda:False,ocr_factory=None):
    manifest=library.manifest(book_id)
    if manifest["kind"]!="comic":
        raise ValueError("框选识别仅用于漫画")
    image=load_image(library.path(book_id)/manifest["units"][index]["path"])
    x,y,w,h=selection_box(box,image.size)
    if cancel():
        raise InterruptedError("框选识别已取消")
    progress(0,1,"正在识别选区内的日文…")
    cache=library.path(book_id)/"cache"
    cache.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="selection-",dir=cache) as temporary:
        crop_path,output=Path(temporary)/"crop.png",Path(temporary)/"ocr.json"
        image.crop((x,y,x+w,y+h)).save(crop_path)
        if ocr_factory:ocr=ocr_factory(library,cancel)
        elif settings.comic_engine=='mtu':
            from .mtu_bridge import MTUProcess
            ocr=MTUProcess(library,cancel,settings.mtu_root,ocr_model=settings.comic_ocr)
        else:ocr=OCRProcess(library,cancel)
        try:
            ocr.recognize(crop_path,output,settings.gpu)
        finally:
            ocr.close()
        if cancel():
            raise InterruptedError("框选识别已取消")
        data=json.loads(output.read_text(encoding="utf-8"))
    regions=[]
    for region in data.get("regions",[]):
        if not region.get("text","").strip():
            continue
        try:
            region["box"]=selection_box(region["box"],(w,h))
        except ValueError:
            continue
        region.update(selection="include",translation="",layout="auto")
        regions.append(region)
    progress(1,1,"识别完成" if regions else "选区内未识别到文字，请调整范围后重试。")
    return {"box":[x,y,w,h],"regions":regions}


def translate_selection(library,book_id,index,selection,settings,progress=lambda *_:None,cancel=lambda:False,translator=None):
    result=deepcopy(selection)
    pending=[r for r in result["regions"] if r.get("text","").strip()]
    if not pending:
        return result
    if cancel():
        raise InterruptedError("框选翻译已取消")
    key=hashlib.sha256(json.dumps(result["box"]).encode()).hexdigest()[:16]
    cloud=translator or create_translator(settings,library,book_id,cancel,kind="comic")
    # No surrounding page text is sent as context.
    translated=cloud.translate([r["text"] for r in pending],library.path(book_id)/"cache"/f"{index:06d}-selection-{key}.json",
                               context="",progress=progress)
    for region,text in zip(pending,translated):
        region["translation"]=text
    from .text_replace import apply_global_replacements
    apply_global_replacements(result,settings)
    if cancel():
        raise InterruptedError("框选翻译已取消，已完成译文保存在缓存中")
    return result


def selection_overwrites(library,book_id,index,box,*,review_key=None):
    target=translated_path(library,book_id,index)
    if review_key:
        from .edition_review import valid_image
        target=valid_image(library,book_id,index,review_key)
        if not target:return False
    if not target.exists():
        return False
    manifest=library.manifest(book_id)
    original=load_image(library.path(book_id)/manifest["units"][index]["path"])
    current=load_image(target)
    if current.size!=original.size:
        if review_key:current=current.resize(original.size)
        else:raise ValueError("已有译图尺寸与原图不一致，请先重新生成译图")
    x,y,w,h=selection_box(box,original.size)
    return current.crop((x,y,x+w,y+h)).tobytes()!=original.crop((x,y,x+w,y+h)).tobytes()


def apply_selection(library,book_id,index,selection,settings,*,review_key=None):
    manifest=library.manifest(book_id)
    source=library.path(book_id)/manifest["units"][index]["path"]
    original=load_image(source)
    result=deepcopy(selection)
    from .text_replace import apply_global_replacements
    apply_global_replacements(result,settings)
    result["box"]=selection_box(result["box"],original.size)
    x,y,w,h=result["box"]
    from .text_replace import has_translated_text
    if not result["regions"] or any(not has_translated_text(r) for r in result["regions"]):
        raise ValueError("请先完成所有文字块的翻译，或移除无需翻译的文字块")
    for region in result["regions"]:
        region["box"]=selection_box(region["box"],(w,h))
        region["selection"]="include"
    if review_key:
        from .edition_review import ensure_page,load_data,render_review
        ensure_page(library,book_id,index)
        edits=load_data(library,book_id,index,review_key)
        patches=edits.setdefault('manual_selections',[])
        previous_box=result.pop('_replace_box',None)
        patches[:]=[p for p in patches if p['box']!=result['box'] and p['box']!=previous_box]
        patches.append(result)
        target=render_review(library,book_id,index,review_key,edits,settings)
        selection.clear();selection.update(deepcopy(edits['manual_selections'][-1]))
        return target
    path=edit_path(library,book_id,index)
    old=json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    cleanup=dict((old or {}).get('residual_cleanup',{}))
    cleanup.setdefault('strength',settings.comic_cleanup_strength)
    patch=render_image(original.crop((x,y,x+w,y+h)),result["regions"],settings.font_path,"all",settings.comic_layout,settings.comic_style,
                       boundaries=PageBoundaries(original),boundary_offset=(x,y),cleanup=cleanup,gpu=settings.gpu)
    target=translated_path(library,book_id,index)
    current=load_image(target) if target.exists() else original.copy()
    if current.size!=original.size:
        raise ValueError("已有译图尺寸与原图不一致")
    if result.get('recognition_box'):
        from .layout_area import composite_selection,covered_by_selection
        previous=[(r,(0,0)) for r in (old or {}).get('regions',[]) if covered_by_selection(r,result)]
        previous.extend((r,p['box'][:2]) for p in (old or {}).get('manual_selections',[])
                        if p['box'] in (result['box'],result.get('_replace_box')) for r in p.get('regions',[]))
        current=composite_selection(original,current,patch,result,previous)
    else:current.paste(patch,(x,y))
    edits=deepcopy(old) if old is not None else {}
    patches=edits.setdefault("manual_selections",[])
    # Replace identical selections, retain earlier partially overlapping patches.
    previous_box=result.pop('_replace_box',None)
    patches[:]=[p for p in patches if p["box"]!=result["box"] and (previous_box is None or p['box']!=previous_box)]
    patches.append(result)
    target.parent.mkdir(parents=True,exist_ok=True)
    from .page_render import stage_page_editions,commit_page_editions
    with tempfile.TemporaryDirectory(prefix='selection-editions-',dir=library.path(book_id)) as folder:
        directory=Path(folder);normal=directory/'normal.png'
        current.save(normal,format='PNG')
        outputs=stage_page_editions(library,book_id,index,edits,settings,normal,directory)
        commit_page_editions(outputs,directory)
    # Expose the committed status only after both edits and image were saved.
    selection.clear();selection.update(deepcopy(result))
    return target


class OCRProcess:
    def __init__(self, library: Library, cancel):
        self.cancel = cancel
        log_path = library.root / "ocr.log"
        self.log = log_path.open("a", encoding="utf-8")
        self.process = None
        self.results = queue.Queue()

    def _start(self):
        if self.process:
            return
        command = [str(app_root()/"阅译OCR.exe"), "--ocr-worker"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "reader.ocr_worker"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(command, cwd=app_root(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.log, text=True, encoding="utf-8", creationflags=flags)

        def read():
            for line in self.process.stdout:
                try:
                    self.results.put(json.loads(line))
                except ValueError:
                    pass
            self.results.put({"ok": False, "error": "识别进程退出，请查看 data/ocr.log"})

        threading.Thread(target=read, daemon=True).start()

    def recognize(self, source: Path, output: Path, gpu=True):
        return self._request(source,output,gpu)

    def supplement_white(self, source: Path, output: Path, gpu, regions):
        return self._request(source,output,gpu,regions)

    def recognize_boxes(self,source,output,gpu,boxes):
        return self._request(source,output,gpu,boxes=boxes)

    def _request(self,source,output,gpu,regions=None,boxes=None):
        self._start()
        request={"path":str(source),"output":str(output),"gpu":gpu}
        if regions is not None:request['existing_regions']=regions
        if boxes is not None:request['forced_boxes']=boxes
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        while True:
            if self.cancel():
                self.close()
                raise InterruptedError("识别已暂停")
            try:
                result = self.results.get(timeout=0.2)
                if not result["ok"]:
                    raise ValueError("本地 OCR 未完成：" + result.get("error", "未知错误"))
                return
            except queue.Empty:
                continue

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process:
            for stream in (self.process.stdin,self.process.stdout):
                if stream:
                    stream.close()
        self.log.close()


def translate_book(library: Library, book_id: str, settings: Settings, indexes: list[int],
                   progress=lambda *_: None, cancel=lambda: False, job_id=None, ocr_only=False,
                   translator=None,reprocess=False,ocr_factory=None,manage_job=True,preserve_upscale=True):
    manifest = library.manifest(book_id)
    root = library.path(book_id)
    if manage_job:job_id = job_id or library.create_job(book_id, "ocr" if ocr_only else "translate", len(indexes), {"indexes": indexes,"reprocess":reprocess})
    translator = translator or create_translator(settings, library, book_id, cancel,kind=manifest["kind"])
    ocr=None
    done = 0
    boundary_warnings=0
    ocr_warnings=0
    layout_warnings=0
    def get_ocr():
        nonlocal ocr
        if ocr is None:
            if manifest['kind']=='comic' and settings.comic_engine=='mtu':
                from .mtu_bridge import MTUProcess
                ocr=MTUProcess(library,cancel,settings.mtu_root,ocr_model=settings.comic_ocr)
            else:ocr=OCRProcess(library,cancel)
            if ocr_factory:ocr=ocr_factory(ocr)
        return ocr
    try:
        for index in indexes:
            if cancel():
                raise InterruptedError("任务已暂停，结果已保存")
            unit = manifest["units"][index]
            if manifest['kind']=='comic':
                from .edition_review import ensure_page
                ensure_page(library,book_id,index)
            path = edit_path(library, book_id, index)
            edits = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            progress(done, len(indexes), f"处理 {unit['title']}" + ("；使用内置 "+(settings.comic_ocr if settings.comic_engine=='mtu' else 'Manga OCR')+" 识别" if manifest["kind"] == "comic" else ""))
            if manifest["kind"] == "comic":
                from .text_replace import has_translated_text,apply_global_replacements
                from .layout_area import covered_by_selection
                def manually_covered(region):
                    return any(covered_by_selection(region,p) for p in edits.get('manual_selections',[]))
                if not reprocess and not ocr_only:
                    from .page_render import saved_page_current
                    apply_global_replacements(edits,settings)
                    missing=any(r.get('text','').strip() and not has_translated_text(r) and
                        region_enabled(r,settings.comic_scope) and not manually_covered(r) for r in edits.get('regions',[]))
                    if not missing and saved_page_current(library,book_id,index,edits,settings,preserve_upscale):
                        from .colorization import sync_existing_colors
                        sync_existing_colors(library,book_id,index,edits)
                        done+=1
                        boundary_warnings+=sum(bool(r.get('boundary_warning')) for r in edits['regions'])
                        ocr_warnings+=sum(bool(r.get('ocr_warning')) and not r.get('ocr_reviewed') for r in edits['regions'])
                        from .layout_status import overflow_count
                        layout_warnings+=overflow_count(edits)
                        if manage_job:library.update_job(job_id,done)
                        progress(done,len(indexes),f"{unit['title']} 已完成，跳过重复处理")
                        continue
                original=load_image(root/unit['path'])
                regroup=edits.get('boundary_version',0)<3 and needs_regrouping(original,edits.get('regions',[]))
                if reprocess or "regions" not in edits or regroup or (not edits["regions"] and edits.get("manual_selections") and not edits.get("ocr_complete")):
                    previous=edits.get('regions',[])
                    if reprocess and path.exists():
                        import shutil
                        from datetime import datetime
                        backup=root/'backups'/f"page-{index:06d}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
                        backup.mkdir(parents=True,exist_ok=True);shutil.copy2(path,backup/path.name)
                        target=translated_path(library,book_id,index)
                        if target.exists():shutil.copy2(target,backup/target.name)
                    if regroup:progress(done,len(indexes),'正在按气泡与分镜边界重新识别，修正旧文字分组…')
                    with tempfile.TemporaryDirectory(prefix="page-ocr-",dir=root) as temporary:
                        output=Path(temporary)/"ocr.json"
                        get_ocr().recognize(root / unit["path"], output, settings.gpu)
                        edits.update(json.loads(output.read_text(encoding="utf-8")))
                        edits["ocr_complete"]=True
                        from .text_replace import has_translated_text
                        for region in edits['regions']:
                            matches=[r for r in previous if not reprocess and r.get('text','').strip()==region.get('text','').strip() and has_translated_text(r)]
                            if len(matches)==1:
                                a=matches[0]['box'];b=region['box']
                                if max(a[0],b[0])<min(a[0]+a[2],b[0]+b[2]) and max(a[1],b[1])<min(a[1]+a[3],b[1]+b[3]):
                                    for key in ('translation','global_replacement','font_size','font_path','style_reviewed','mode','color','layout','layout_box','weight','selection'):
                                        if key in matches[0]:region[key]=matches[0][key]
                        edits['boundary_version']=3
                        if reprocess:
                            # Explicitly reviewed source/translation takes precedence over new OCR.
                            reviewed=[r for r in previous if r.get('ocr_reviewed') or r.get('style_reviewed') or r.get('selection') in ('include','exclude')]
                            for old in reviewed:
                                x,y,w,h=old['box']
                                def overlaps(r):
                                    a,b,c,d=r['box'];intersection=max(0,min(x+w,a+c)-max(x,a))*max(0,min(y+h,b+d)-max(y,b))
                                    return intersection/max(1,min(w*h,c*d))>.5
                                matched=next((r for r in edits['regions'] if overlaps(r)),{})
                                restored=deepcopy(old)
                                for key in ('mtu_mask','mtu_bubble','mtu_gpu'):
                                    if key in matched:restored[key]=matched[key]
                                edits['regions']=[r for r in edits['regions'] if not overlaps(r)]+[restored]
                            if settings.comic_engine=='mtu':
                                for r in edits['regions']:r.update(processing_engine='mtu',mtu_root=settings.mtu_root)
                            edits['regions'].sort(key=lambda r:(int(r['box'][1]/120),-r['box'][0]))
                if edits.get('white_scan_version',0)<1:
                    if white_frame_boxes(original,edits['regions'],missing_frames_only=True):
                        progress(done,len(indexes),'正在优先补查遗漏的白底文字框…')
                        with tempfile.TemporaryDirectory(prefix='white-ocr-',dir=root) as temporary:
                            output=Path(temporary)/'ocr.json'
                            get_ocr().supplement_white(root/unit['path'],output,settings.gpu,edits['regions'])
                            edits.update(json.loads(output.read_text(encoding='utf-8')))
                    edits['white_scan_version']=1
                if not ocr_only:
                    annotate_regions(load_image(root / unit["path"]), edits["regions"])
                    annotate_boundaries(original,edits['regions'])
                    from .text_replace import has_translated_text
                    pending = [region for region in edits["regions"] if region.get("text", "").strip() and not has_translated_text(region) and region_enabled(region, settings.comic_scope)]
                    pending=[region for region in pending if not manually_covered(region)]
                    if len(pending)>1:pending=prioritize_white_regions(original,pending)
                    if pending:
                        texts = [region["text"] for region in pending]
                        context=manifest['title']+'\n本页原文（按阅读顺序）：\n'+'\n'.join(r.get('text','') for r in edits['regions'])
                        for prior in range(max(0,index-2),index):
                            prior_path=edit_path(library,book_id,prior)
                            if prior_path.exists():
                                prior_edits=json.loads(prior_path.read_text('utf-8'))
                                context+='\n前页对白：\n'+'\n'.join(r.get('text','')+' → '+r.get('translation','') for r in prior_edits.get('regions',[]))
                        result = translator.translate(texts, root / "cache" / f"{index:06d}-comic.json",
                                                      context=context, progress=lambda a,b,msg: progress(done,len(indexes),msg))
                        for region, text in zip(pending, result):
                            region["translation"] = text
                    # Save recognized/translated text even if image processing later fails.
                    write_json(path, edits)
                    from .page_render import render_saved_page
                    render_saved_page(library,book_id,index,edits,settings,cancel=cancel,renderer=render_page,
                                      preserve_upscale=preserve_upscale)
                else:
                    write_json(path,edits)
            else:
                if ocr_only:
                    raise ValueError("小说无需图片识别")
                source = unit["paragraphs"]
                edits.setdefault("translations", [""] * len(source))
                pending = [i for i,text in enumerate(source) if text.strip() and not edits["translations"][i] and not edits.get('global_replacement_paragraphs',{}).get(str(i),{}).get('base')]
                translate_title=not (edits.get("title_translation") or edits.get('global_replacement_title',{}).get('base'))
                if pending or translate_title:
                    texts = ([unit["title"]] if translate_title else [])+[source[i] for i in pending]
                    context = "\n".join(manifest["units"][max(0,index-1)]["paragraphs"][-3:]) if index else manifest["title"]
                    result = translator.translate(texts, root / "cache" / f"{index:06d}-novel.json", context=context,
                                                  progress=lambda a,b,msg: progress(done,len(indexes),msg))
                    if translate_title:
                        edits["title_translation"]=result[0]
                        result=result[1:]
                    for paragraph, text in zip(pending, result):
                        edits["translations"][paragraph] = text
                from .text_replace import apply_global_replacements
                apply_global_replacements(edits,settings)
                write_json(path, edits)
            done += 1
            if manage_job:library.update_job(job_id, done)
            warnings=sum(bool(r.get('boundary_warning')) for r in edits.get('regions',[]))
            boundary_warnings+=warnings
            weak=sum(bool(r.get('ocr_warning')) and not r.get('ocr_reviewed') for r in edits.get('regions',[]));ocr_warnings+=weak
            from .layout_status import overflow_count
            overflow=overflow_count(edits);layout_warnings+=overflow
            progress(done, len(indexes), f"完成 {unit['title']}"+(f'；{warnings} 个文字框边界待确认，保留原文' if warnings else '')+(f'；{weak} 个低置信度原文待校对' if weak else '')+(f'；{overflow} 个译文放不下，已标红待校对' if overflow else ''))
        if manage_job:library.update_job(job_id, done, "complete", "完成"+(f'；{boundary_warnings} 个文字框边界待确认，保留原文，可框选翻译' if boundary_warnings else '')+(f'；{ocr_warnings} 个低置信度原文待校对' if ocr_warnings else '')+(f'；{layout_warnings} 个译文放不下，已标红待校对' if layout_warnings else ''))
        return book_id
    except InterruptedError as error:
        if manage_job:library.update_job(job_id, done, "paused", str(error))
        raise
    except Exception as error:
        if manage_job:library.update_job(job_id, done, "failed", str(error))
        raise
    finally:
        if ocr:ocr.close()
