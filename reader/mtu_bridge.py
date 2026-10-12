"""MTU integration with isolated runtimes and the reader's strict border rules."""
from __future__ import annotations
import hashlib,json,os,queue,subprocess,sys,tempfile,threading,time
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy
import cv2
import numpy as np
from PIL import Image
from .config import Settings,app_root,data_root,write_json
from .embedded_runtime import manga_root,resolve_font,child_environment
from .comic_regions import PageBoundaries,annotate_regions,annotate_boundaries,region_enabled,largest_inner_rectangle,text_orientation,white_frame_boxes
from .layout_status import mark_overflow,clear_overflow

DEFAULT_MTU=manga_root()


def mtu_root(value=''):
    # Historical edits/settings can contain the old MTU path. Always resolve
    # the owned engine beside this executable, including after relocation.
    root=manga_root()
    if not (root/'packaging/python/python.exe').is_file() or not (root/'manga_translator/config.py').is_file():
        raise ValueError('内置漫画引擎缺失，请恢复完整阅译程序目录（包含 engines），或切换到原有漫画流程。')
    for file in ('models/detection/detect-20241225.ckpt','models/detection/ysgyolo_yolo26_2.0.pt',
                 'models/ocr/ocr_ar_48px.ckpt','models/ocr/alphabet-all-v7.txt','models/inpainting/lamalarge.onnx','models/detection/mangalens.pt'):
        if not (root/file).is_file():raise ValueError('内置漫画模型缺失：'+file+'；请恢复 engines 文件夹。')
    return root


def owned_cache_file(value):
    """Ignore absolute cache paths outside this portable copy after a move."""
    if not value:return Path('__missing_cache__')
    path=Path(value).resolve()
    if path.is_relative_to(app_root().resolve()) or path.is_relative_to(data_root().resolve()):return path
    return Path('__missing_cache__')


def convert_blocks(image,blocks,ocr_model='48px'):
    boxes=[]
    for block in blocks:
        for points in block['lines']:
            p=np.asarray(points);a=p.min(axis=0);b=p.max(axis=0)
            boxes.append([int(a[0]),int(a[1]),max(1,int(b[0]-a[0])),max(1,int(b[1]-a[1]))])
    bounds=PageBoundaries(image,boxes);groups={}
    for index,block in enumerate(blocks):
        for line,points in enumerate(block['lines']):
            p=np.asarray(points);a=p.min(axis=0);b=p.max(axis=0)
            x,y=max(0,int(a[0])),max(0,int(a[1]));w=max(1,min(image.width,int(b[0])+1)-x);h=max(1,min(image.height,int(b[1])+1)-y)
            owner=bounds.owner([x,y,w,h]);key=('frame',owner) if owner else ('block',index)
            text=block.get('texts',[])[line] if line<len(block.get('texts',[])) else block.get('text','')
            if not text.strip():continue
            groups.setdefault(key,[]).append((x,y,w,h,text,points,block))
    regions=[]
    for group in groups.values():
        vertical=sum(r[3]>r[2]*1.15 for r in group)>=len(group)/2
        group.sort(key=(lambda r:(-round((r[0]+r[2]/2)/12),r[1])) if vertical else (lambda r:(r[1],r[0])))
        x=max(0,min(r[0] for r in group)-3);y=max(0,min(r[1] for r in group)-3)
        right=min(image.width,max(r[0]+r[2] for r in group)+3);bottom=min(image.height,max(r[1]+r[3] for r in group)+3)
        regions.append({'box':[x,y,right-x,bottom-y],'text':''.join(r[4] for r in group),'translation':'',
            'source_font_size':round(float(np.median([r[6].get('font_size',20) for r in group]))),
            'source_orientation':'vertical' if vertical else 'horizontal','text_polygons':[r[5] for r in group],
            'ocr_confidence':float(np.mean([r[6].get('prob',1) for r in group])),
            'font_size':0,'mode':'inpaint','color':'#111111','processing_engine':'mtu','ocr_engine':'mtu-'+ocr_model})
    regions.sort(key=lambda r:(int(r['box'][1]/120),-r['box'][0]))
    annotate_regions(image,regions);annotate_boundaries(image,regions)
    return regions


def deduplicate_mocr_regions(image,regions,*,bounds=None):
    """Drop inferior retries of text already contained in the same balloon."""
    bounds=bounds if bounds is not None else PageBoundaries(image,[r['box'] for r in regions])
    kept=[]
    for region in sorted(regions,key=lambda r:r['box'][2]*r['box'][3],reverse=True):
        if region.get('ocr_reviewed') or region.get('style_reviewed') or region.get('selection') in ('include','exclude'):
            kept.append(region);continue
        x,y,w,h=region['box'];owner=bounds.owner(region['box'])
        duplicate=False
        for earlier in kept:
            a,b,c,d=earlier['box']
            overlap=max(0,min(x+w,a+c)-max(x,a))*max(0,min(y+h,b+d)-max(y,b))
            if overlap/max(1,w*h)<.85:continue
            if not owner or bounds.owner(earlier['box'])!=owner:continue
            # Manga OCR reads the entire crop. Tiny detections of individual
            # strokes inside it must not become a second independent dialogue.
            tiny_retry=(max(w,h)<=12 and w*h<=c*d*.05 and region.get('source_font_size',20)<4)
            if (tiny_retry or region.get('text','') in earlier.get('text','') or
                earlier.get('ocr_confidence',0)>=region.get('ocr_confidence',0)+.1):
                duplicate=True;break
        if not duplicate:kept.append(region)
    identities={id(r) for r in kept}
    return [r for r in regions if id(r) in identities]


class MTUProcess:
    def __init__(self,library,cancel=lambda:False,root='',ocr_model='48px'):
        if ocr_model not in ('48px','mocr'):raise ValueError('不支持的漫画 OCR 模型：'+str(ocr_model))
        self.ocr_model=ocr_model
        self.library,self.cancel,self.root=library,cancel,mtu_root(root)
        if ocr_model=='mocr':
            for name in ('config.json','preprocessor_config.json','pytorch_model.bin','special_tokens_map.json','tokenizer_config.json','vocab.txt'):
                if not (self.root/'models/ocr/manga_ocr'/name).is_file():
                    raise ValueError('内置 mocr 模型缺失：'+name+'；请恢复完整 engines 文件夹。')
        self.process=None;self.results=queue.Queue()
        library.root.mkdir(parents=True,exist_ok=True)
        self.log=(library.root/'mtu.log').open('a',encoding='utf-8')

    def _start(self):
        if self.process:return
        worker=(Path(sys._MEIPASS)/'mtu_worker.py') if getattr(sys,'frozen',False) else Path(__file__).with_name('mtu_worker.py')
        cache=(self.library.root/'mtu-cache').resolve()
        for folder in ('yolo','matplotlib'):(cache/folder).mkdir(parents=True,exist_ok=True)
        env=child_environment();env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',QT_QPA_PLATFORM='offscreen',
          YOLO_CONFIG_DIR=str(cache/'yolo'),MPLCONFIGDIR=str(cache/'matplotlib'),
          HF_HOME=str(cache/'huggingface'),HF_HUB_CACHE=str(cache/'huggingface/hub'),
          HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
        # PyInstaller's Qt environment belongs to PySide, not the independent PyQt sidecar.
        for key in ('QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH','PYTHONHOME','PYTHONPATH'):env.pop(key,None)
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        # Safe-path mode prevents the script's _internal directory from becoming
        # sys.path[0]. Otherwise Python 3.12 can import the bundled Python 3.13
        # _socket.pyd before its own standard-library extension.
        command=[str(self.root/'packaging/python/python.exe'),'-I','-X','utf8','-B',str(worker),str(self.root)]
        dll_directory=None
        if os.name=='nt' and getattr(sys,'frozen',False):
            import ctypes
            buffer=ctypes.create_unicode_buffer(32768);ctypes.windll.kernel32.GetDllDirectoryW(len(buffer),buffer)
            dll_directory=buffer.value;ctypes.windll.kernel32.SetDllDirectoryW(None)
        try:
            self.process=subprocess.Popen(command,cwd=app_root(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,
                    text=True,encoding='utf-8',env=env,creationflags=flags)
        finally:
            if dll_directory is not None:ctypes.windll.kernel32.SetDllDirectoryW(dll_directory)
        def read():
            for line in self.process.stdout:
                try:self.results.put(json.loads(line))
                except ValueError:pass
            self.results.put({'ok':False,'error':'MTU 子进程退出，请查看 mtu.log'})
        threading.Thread(target=read,daemon=True).start()

    def request(self,action,**data):
        if action in ('analyze','info'):data['ocr_model']=self.ocr_model
        self._start();self.process.stdin.write(json.dumps({'action':action,**data})+'\n');self.process.stdin.flush()
        while True:
            if self.cancel():self.close();raise InterruptedError('任务已暂停，旧译图已保留')
            try:result=self.results.get(timeout=.2)
            except queue.Empty:continue
            if not result.get('ok'):raise ValueError('MTU 处理未完成：'+result.get('error','未知错误'))
            return result

    def recognize(self,source,output,gpu=True):
        self.gpu=gpu
        cache=self.library.root/'mtu-cache';cache.mkdir(exist_ok=True)
        digest=hashlib.sha256(Path(source).read_bytes()).hexdigest()
        mask=cache/(digest+'-mask.png')
        bubble=cache/(digest+'-bubble.png')
        result=self.request('analyze',path=str(source),gpu=gpu,mask_output=str(mask),bubble_output=str(bubble))
        with Image.open(source) as opened:image=opened.convert('RGB')
        regions=convert_blocks(image,result['blocks'],self.ocr_model)
        bounds=PageBoundaries(image,[r['box'] for r in regions])
        partial=white_frame_boxes(image,regions)
        retries={}
        for box in partial+[r['box'] for r in regions if r['ocr_confidence']<.55]:
            owner=bounds.owner(box)
            if owner and bounds.tone(owner)=='light' and any(bounds.owner(r['box'])==owner for r in regions):retries[owner]=box
        for owner in retries:
            sx,sy,m=bounds.shape(owner);x,y=int(sx/bounds.scale),int(sy/bounds.scale)
            w=min(image.width-x,round(m.shape[1]/bounds.scale));h=min(image.height-y,round(m.shape[0]/bounds.scale))
            patch_path=cache/(digest+f'-retry-{owner}.png')
            image.crop((x,y,x+w,y+h)).resize((w*2,h*2)).save(patch_path)
            refined=self.request('analyze',path=str(patch_path),gpu=gpu,ocr_prob=0)
            for block in refined['blocks']:
                block['lines']=(np.asarray(block['lines'])/2+[x,y]).tolist();block['font_size']=block.get('font_size',20)/2
            improved=convert_blocks(image,refined['blocks'],self.ocr_model)
            same=[r for r in regions if bounds.owner(r['box'])==owner]
            if improved and sum(len(r['text']) for r in improved)>=sum(len(r['text']) for r in same) and np.mean([r['ocr_confidence'] for r in improved])>=.25:
                regions=[r for r in regions if bounds.owner(r['box'])!=owner]+improved
        missing=uncovered_white_lines(image,regions)
        if missing:
            extra=self.request('analyze',path=str(source),gpu=gpu,ocr_prob=.1,forced_polygons=missing)
            recovered=convert_blocks(image,extra['blocks'],self.ocr_model)
            for candidate in recovered:
                containing=[]
                cx,cy,cw,ch=candidate['box']
                for frame in white_frame_boxes(image,regions):
                    x,y,w,h=frame
                    if x<=cx+cw/2<x+w and y<=cy+ch/2<y+h:
                        containing=[r for r in regions if x<=r['box'][0]+r['box'][2]/2<x+w and y<=r['box'][1]+r['box'][3]/2<y+h]
                        break
                if len(containing)==1:
                    r=containing[0];a,b,c,d=r['box'];parts=sorted([r,candidate],key=lambda q:-q['box'][0])
                    r['text']=''.join(q['text'] for q in parts);r['text_polygons']=r.get('text_polygons',[])+candidate.get('text_polygons',[])
                    r['box']=[min(a,cx),min(b,cy),max(a+c,cx+cw)-min(a,cx),max(b+d,cy+ch)-min(b,cy)]
                    r['ocr_confidence']=min(r['ocr_confidence'],candidate['ocr_confidence'])
                    r['ocr_engine']='mtu-'+self.ocr_model+' / white-column-recovery'
                else:regions.append(candidate)
        # Retry weak lines directly with padding, without running the detector
        # again: a clipped first glyph can otherwise remain clipped at any scale.
        weak=[r for r in regions if r['ocr_confidence']<.5]
        if weak:
            padded=[]
            for r in weak:
                x,y,w,h=r['box'];padded.append([max(0,x-5),max(0,y-8),min(image.width-max(0,x-5),w+10),min(image.height-max(0,y-8),h+16)])
            refined=self.request('analyze',path=str(source),gpu=gpu,ocr_prob=.1,forced_boxes=padded)
            for candidate in convert_blocks(image,refined['blocks'],self.ocr_model):
                x,y,w,h=candidate['box']
                match=max(weak,key=lambda r:max(0,min(x+w,r['box'][0]+r['box'][2])-max(x,r['box'][0]))*max(0,min(y+h,r['box'][1]+r['box'][3])-max(y,r['box'][1])))
                if candidate['ocr_confidence']>match['ocr_confidence']+.03 and len(candidate['text'])>=len(match['text']):match.update(candidate)
        # Retain the white-frame recovery that the user requested, alongside MTU.
        if white_frame_boxes(image,regions,missing_frames_only=True):
            if self.ocr_model=='mocr':
                regions=self._supplement_mocr(source,image,gpu,regions)
            else:
                from .pipeline import OCRProcess
                classic=OCRProcess(self.library,self.cancel)
                try:
                    classic.supplement_white(source,output,gpu,regions)
                    regions=json.loads(Path(output).read_text('utf-8'))['regions']
                finally:classic.close()
        if self.ocr_model=='mocr':regions=deduplicate_mocr_regions(image,regions)
        for region in regions:
            region.update(processing_engine='mtu',mtu_root=str(self.root),mtu_mask=str(mask),mtu_bubble=str(bubble),mtu_gpu=gpu)
            if region.get('ocr_confidence',1)<.5:region['ocr_warning']='手写或低置信度文字，请在校对中核对原文。'
        write_json(Path(output),{'regions':regions,'width':image.width,'height':image.height,
                 'boundary_version':3,'white_scan_version':1,'comic_engine':'mtu','engine_version':1,
                 'ocr_model':self.ocr_model,'ocr_device':result['device']})

    def _supplement_mocr(self,source,image,gpu,regions):
        boxes=white_frame_boxes(image,regions,missing_frames_only=True)
        if not boxes:return regions
        result=self.request('analyze',path=str(source),gpu=gpu,ocr_prob=0,forced_boxes=boxes)
        added=convert_blocks(image,result['blocks'],'mocr')
        for region in added:region['ocr_engine']='mtu-mocr / white-frame-recovery'
        return regions+added

    def supplement_white(self,source,output,gpu,regions):
        if self.ocr_model=='mocr':
            with Image.open(source) as opened:image=opened.convert('RGB')
            recovered=self._supplement_mocr(source,image,gpu,regions)
            write_json(Path(output),dict(regions=recovered,width=image.width,height=image.height,
                        boundary_version=3,white_scan_version=1,comic_engine='mtu',ocr_model='mocr'))
            return
        from .pipeline import OCRProcess
        classic=OCRProcess(self.library,self.cancel)
        try:classic.supplement_white(source,output,gpu,regions)
        finally:classic.close()

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        if self.process:
            for stream in (self.process.stdin,self.process.stdout):
                if stream:stream.close()
        self.log.close()


def uncovered_white_lines(image,regions):
    """Recover only uncovered glyph columns inside confirmed white frames."""
    bounds=PageBoundaries(image,[r['box'] for r in regions]);gray=np.asarray(image.convert('L'))
    known=np.zeros(gray.shape,np.uint8)
    for r in regions:
        x,y,w,h=r['box'];known[max(0,y-3):min(image.height,y+h+3),max(0,x-3):min(image.width,x+w+3)]=1
    polygons=[]
    for box in white_frame_boxes(image,regions):
        owner=bounds.owner(box)
        x,y,w,h=box
        enclosed=[r for r in regions if x<=r['box'][0]+r['box'][2]/2<x+w and y<=r['box'][1]+r['box'][3]/2<y+h]
        if not owner or not enclosed:continue
        x,y,w,h=box;safe=bounds.shape_crop(owner,box)>0
        raw_ink=(gray[y:y+h,x:x+w]<150).astype(np.uint8)
        _,ink_labels=cv2.connectedComponents(raw_ink,8)
        edge=np.zeros_like(raw_ink,bool);edge[[0,-1],:]=True;edge[:,[0,-1]]=True
        rim_ids=np.unique(ink_labels[edge&(raw_ink>0)])
        ink=((raw_ink>0)&~np.isin(ink_labels,rim_ids)&safe&(known[y:y+h,x:x+w]==0)).astype(np.uint8)
        size=int(np.median([r.get('source_font_size',20) for r in enclosed]))
        grouped=cv2.dilate(ink,np.ones((max(13,min(45,size)),5),np.uint8))
        count,labels,stats,_=cv2.connectedComponentsWithStats(grouped,8)
        for n in range(1,count):
            yy,xx=np.nonzero((labels==n)&(ink>0))
            if len(xx)<35 or np.ptp(yy)<18 or np.ptp(xx)<4:continue
            points=np.column_stack((xx+x,yy+y)).astype(np.float32)
            center,dimensions,angle=cv2.minAreaRect(points)
            polygon=cv2.boxPoints((center,(dimensions[0]+6,dimensions[1]+6),angle))
            if polygon[:,0].min()<x-5 or polygon[:,0].max()>x+w+5 or polygon[:,1].min()<y-5 or polygon[:,1].max()>y+h+5:continue
            polygons.append(polygon.tolist())
            known[y+yy,x+xx]=1
    return polygons


def manual_dark_space(bounds,gray,region,bubble):
    """Recover a manually confirmed dark balloon inside its actual dark contour."""
    if region.get('selection')!='include' or bounds.text_tone(region['box'])!='dark' or not bubble.is_file():return None
    with Image.open(bubble) as opened:semantic=np.asarray(opened.convert('L').resize((bounds.width,bounds.height)))>0
    _,labels,stats,_=cv2.connectedComponentsWithStats(semantic.astype(np.uint8),8)
    x,y,w,h=map(int,region['box']);sample=labels[max(0,y):y+h,max(0,x):x+w]
    ids,counts=np.unique(sample[sample>0],return_counts=True)
    if not len(ids):return None
    label=ids[int(np.argmax(counts))]
    if np.count_nonzero(sample==label)/max(1,sample.size)<.80:return None
    # An AI mask alone cannot authorize expansion. Follow the original black
    # component seeded by this text, filling only its enclosed white glyph holes.
    _,dark_labels=cv2.connectedComponents(((gray<100)&(labels==label)).astype(np.uint8),8)
    sample=dark_labels[max(0,y):y+h,max(0,x):x+w]
    ids,counts=np.unique(sample[sample>0],return_counts=True)
    if not len(ids):return None
    component=(dark_labels==ids[int(np.argmax(counts))]).astype(np.uint8)
    contours,_=cv2.findContours(component,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    safe=np.zeros_like(component);cv2.drawContours(safe,contours,-1,1,cv2.FILLED)
    safe&=(labels==label).astype(np.uint8)
    safe=cv2.erode(safe,np.ones((3,3),np.uint8))
    left,top,right,bottom=bounds.panel(region['box'])
    yy,xx=np.ogrid[:bounds.height,:bounds.width]
    safe&=((xx>=left+2)&(xx<right-2)&(yy>=top+2)&(yy<bottom-2)).astype(np.uint8)
    ys,xs=np.nonzero(safe)
    if not len(xs):return None
    box=[int(xs.min()),int(ys.min()),int(np.ptp(xs)+1),int(np.ptp(ys)+1)]
    a,b,c,d=box
    return box,safe[b:b+d,a:a+c]


def build_plans(image,regions,scope,layout,font_path,cleanup=None,*,indexes=None,eligibility=None):
    from .text_replace import has_translated_text
    from .comic_regions import cached_boundaries
    bounds=cached_boundaries(image,[r['box'] for r in regions]);annotate_boundaries(image,regions,bounds)
    gray=np.asarray(image.convert('L'));erase=np.zeros(gray.shape,np.uint8);plans=[]
    # Old saved OCR may still contain a tiny stroke retry inside a full text box.
    # Keep its data for correction, but do not allocate a separate rendering area.
    automatic=[r for r in regions if r.get('ocr_engine','').startswith('mtu-mocr')]
    retained={id(r) for r in deduplicate_mocr_regions(image,automatic,bounds=bounds)} if automatic else set()
    automatic_ids={id(r) for r in automatic}
    eligible=[r for r in regions if id(r) not in automatic_ids or id(r) in retained]
    eligible_ids={id(r) for r in eligible}
    if eligibility is not None:
        eligibility.update(i for i,r in enumerate(regions) if id(r) in eligible_ids and
            has_translated_text(r) and region_enabled(r,scope))
    for r in automatic:
        if id(r) not in retained:
            r['render_warning']='重复识别框，已由包含它的文字框排版；可校对后手动设为“翻译此框”。'
        else:r.pop('render_warning',None)
    owner_counts={}
    for r in eligible:
        if has_translated_text(r) and region_enabled(r,scope):
            owner=bounds.owner(r['box']);owner_counts[owner]=owner_counts.get(owner,0)+1
    for index,r in enumerate(regions):
        if indexes is not None and index not in indexes:continue
        if id(r) not in eligible_ids:continue
        if not has_translated_text(r) or not region_enabled(r,scope):
            clear_overflow(r);continue
        font=resolve_font(r.get('font_path') or font_path)
        if not Path(font).is_file():raise ValueError(f'第 {index+1} 个文字框的字体不存在，请在校对界面重新选择字体；旧译图已保留')
        x,y,w,h=map(int,r['box']);owner=bounds.owner(r['box'])
        if r.get('layout_box'):
            from .layout_area import clipped_box,layout_mask
            box=clipped_box(r['layout_box'],image.size);safe,_=layout_mask(bounds,r['box'],box)
        elif owner and owner_counts[owner]==1:
            sx,sy,shape=bounds.shape(owner)
            box=[int(sx/bounds.scale),int(sy/bounds.scale),min(image.width-int(sx/bounds.scale),round(shape.shape[1]/bounds.scale)),
                 min(image.height-int(sy/bounds.scale),round(shape.shape[0]/bounds.scale))]
            safe=bounds.shape_crop(owner,box)
        else:box=r['box'];safe,_=bounds.allowed(box)
        bubble=owned_cache_file(r.get('mtu_bubble',''))
        recovered=manual_dark_space(bounds,gray,r,bubble) if not owner and not r.get('layout_box') else None
        if recovered is not None:box,safe=recovered
        bx,by,bw,bh=largest_inner_rectangle(safe)
        if bw<6 or bh<6:
            mark_overflow(r,'没有足够的安全排版空间，请调整文字框或排版后重新生成。');continue
        full_safe=np.zeros(gray.shape,np.uint8);a,b,c,d=map(int,box);full_safe[b:b+d,a:a+c]=safe*255
        if owner and bubble.is_file() and not r.get('layout_box'):
            with Image.open(bubble) as opened:semantic=np.asarray(opened.convert('L').resize(image.size))>0
            _,labels=cv2.connectedComponents(semantic.astype(np.uint8),8)
            sample=labels[max(0,y):min(image.height,y+h),max(0,x):min(image.width,x+w)]
            ids,counts=np.unique(sample[sample>0],return_counts=True)
            if len(ids):
                label=ids[int(np.argmax(counts))]
                if (sample==label).mean()>.5:
                    full_safe[labels!=label]=0
                    safe=full_safe[b:b+d,a:a+c]//255
                    bx,by,bw,bh=largest_inner_rectangle(safe)
                    if bw<6 or bh<6:
                        mark_overflow(r,'气泡内空间不足，请调整文字框或排版后重新生成。');continue
        pol=np.zeros(gray.shape,np.uint8)
        for points in r.get('text_polygons',[]):cv2.fillPoly(pol,[np.round(points).astype(np.int32)],255)
        if not pol.any():pol[max(0,y):min(image.height,y+h),max(0,x):min(image.width,x+w)]=255
        reach=11 if (cleanup or {}).get('strength')=='enhanced' else 7
        pol=cv2.dilate(pol,np.ones((reach,reach),np.uint8))
        dark=bounds.text_tone(r['box'])=='dark'
        fg=(gray>145) if dark else (gray<150)
        raw=owned_cache_file(r.get('mtu_mask',''))
        if raw.is_file() and not dark:
            with Image.open(raw) as m:learned=np.asarray(m.convert('L').resize(image.size))>0
            fg=fg|learned
        # A detector can crop a few strokes from the last kana. Once a connected
        # glyph is seeded by its text polygon, erase the entire glyph inside the
        # permitted frame instead of leaving those strokes beside the Chinese.
        foreground=(fg&(full_safe>0)).astype(np.uint8)
        _,components=cv2.connectedComponents(foreground,8)
        ids=np.unique(components[(pol>0)&(foreground>0)])
        ink=cv2.dilate(np.isin(components,ids).astype(np.uint8),np.ones((3,3),np.uint8)) if len(ids) else np.zeros_like(foreground)
        erase|=ink*(full_safe>0).astype(np.uint8)*255
        fill=None
        if r.get('mode') in ('white','solid'):
            fill=[255,255,255] if r['mode']=='white' else np.median(np.asarray(image)[full_safe>0],axis=0).astype(np.uint8).tolist()
            erase|=full_safe
        from .images import source_typography
        reference_size,bold=source_typography(image,r)
        size=r.get('font_size') or r.get('source_font_size') or reference_size
        plans.append({'id':index,'rectangle':[a+bx,b+by,bw,bh],'safe':full_safe,'custom_layout':bool(r.get('layout_box')),
          'size':max(8,min(128*r.get('render_scale',1),int(size))),'font':font,'source':r.get('text',''),'translation':r['translation'],
          'weight':('bold' if bold else 'regular') if r.get('weight','auto')=='auto' else r['weight'],'fill':fill,
          'direction':text_orientation(r,layout),'color':[245,245,245] if dark else list(ImageColor_rgb(r.get('color','#111111'))),
          'background':[0,0,0] if dark else [255,255,255]})
    # Physical contours can nest or connect through a one-pixel gap. Give each
    # overlapping pixel to its nearest source text, so one balloon cannot paint
    # over another balloon's translation or steal its larger inner rectangle.
    if plans:
        yy,xx=np.indices(gray.shape);distance=np.full(gray.shape,np.inf);assigned=np.full(gray.shape,-1,np.int32)
        for n,plan in enumerate(plans):
            x,y,w,h=regions[plan['id']]['box']
            dx=np.maximum(np.maximum(x-xx,xx-(x+w)),0);dy=np.maximum(np.maximum(y-yy,yy-(y+h)),0)
            candidate=dx*dx+dy*dy
            take=(plan['safe']>0)&(candidate<distance)
            assigned[take]=n;distance[take]=candidate[take]
        conflicts=[]
        for n,plan in enumerate(plans):
            plan['safe']=(assigned==n).astype(np.uint8)*255
            x,y,w,h=largest_inner_rectangle(plan['safe']>0)
            if w<6 or h<6:
                conflicts.append(plan)
                mark_overflow(regions[plan['id']],'相邻文字框没有足够的独立排版空间，请校对文字框范围。')
            plan['rectangle']=[x,y,w,h]
        # A manually confirmed small box can sit inside another text box. Keep
        # both originals instead of letting one rendering cover the pending box.
        if conflicts:
            blocked={p['id'] for p in conflicts}
            for pending in conflicts:
                x,y,w,h=map(int,regions[pending['id']]['box'])
                for plan in plans:
                    if np.any(plan['safe'][max(0,y):y+h,max(0,x):x+w]):
                        blocked.add(plan['id'])
                        mark_overflow(regions[plan['id']],'相邻文字框空间重叠，请分别调整文字框后重新生成。')
            plans=[p for p in plans if p['id'] not in blocked]
        permitted=np.logical_or.reduce([p['safe']>0 for p in plans]) if plans else np.zeros(gray.shape,bool)
        erase*=permitted.astype(np.uint8)
    return erase,plans


def ImageColor_rgb(color):
    from PIL import ImageColor
    return ImageColor.getrgb(color)


def render_mtu_page(source,regions,destination,font_path,scope,layout,manual_selections=None,backend=None,*,cleanup=None,clean_only=False,incremental=None):
    font_path=resolve_font(font_path)
    from .translation import clean_translation
    from .images import render_image
    source,destination=Path(source),Path(destination)
    with Image.open(source) as im:image=im.convert('RGB')
    for r in regions:
        if r.get('translation') and (not r.get('global_replacement') or r['translation']!=r['global_replacement'].get('result')):
            r['translation']=clean_translation(r['translation'],r.get('text',''))
    from . import incremental_render as fast
    geometry_started=time.perf_counter()
    cacheable=incremental and not clean_only and not (cleanup or {}).get('strokes')
    key=fast.geometry_key(source,regions,scope,cleanup,manual_selections) if cacheable else None
    state=fast.load_state(incremental,key,regions,font_path,layout) if cacheable else None
    activation=False;added_plans=None;review_reuse=False;removed_ids=set()
    if cacheable and not state:
        candidate=fast.load_activation_state(incremental,source,regions,scope,cleanup,manual_selections,font_path,layout)
        if candidate:
            erase,added_plans=build_plans(image,regions,scope,layout,font_path,cleanup,indexes=set(candidate['activation_ids']))
            if fast.activation_plans_disjoint(candidate,added_plans,regions):
                state=candidate;activation=True
        if not state:
            candidate=fast.load_review_state(incremental,source,regions,scope,cleanup,manual_selections,font_path,layout)
            if candidate:
                old_ids={p['id'] for p in candidate['plans']}
                from .text_replace import has_translated_text
                removed={i for i in old_ids if not has_translated_text(regions[i])}
                eligible=set()
                # Shape inputs are unchanged; confirmation flags can only affect
                # retention/eligibility. Verify old owners remain eligible, then
                # reject any new mask that overlaps their previously safe area.
                if fast.removal_only_transition(candidate,regions,removed):
                    erase=np.zeros((image.height,image.width),np.uint8);additions=[];eligible=old_ids-removed
                else:
                    erase,additions=build_plans(image,regions,scope,layout,font_path,cleanup,
                        indexes=set(range(len(regions)))-old_ids,eligibility=eligible)
                if (old_ids-removed<=eligible and {p['id'] for p in additions}==eligible-old_ids
                        and fast.activation_plans_disjoint(candidate,additions,regions)
                        and fast.removal_plans_disjoint(candidate,removed)):
                    candidate['activation_ids']=[p['id'] for p in additions]
                    candidate['dirty']=sorted(set(candidate['dirty'])|removed)
                    state=candidate;activation=True;review_reuse=True;added_plans=additions;removed_ids=removed
                    permitted=np.logical_or.reduce([p['safe']>0 for p in additions]) if additions else np.zeros(erase.shape,bool)
                    erase*=permitted.astype(np.uint8)
    if state and manual_selections:
        dirty=set(state['dirty'])
        for plan in state['plans']:
            if plan['id'] not in dirty:continue
            a,b,c,d=plan['safe_bounds']
            if any(max(a,p['box'][0])<min(a+c,p['box'][0]+p['box'][2]) and
                   max(b,p['box'][1])<min(b+d,p['box'][1]+p['box'][3]) for p in manual_selections):
                state=None;activation=False;break
    if not state:removed_ids=set()
    if state and activation:
        plans=fast.cached_plans(state,regions,font_path,layout,image.size)+added_plans
        current_status=[{k:deepcopy(r[k]) for k in fast.STATUS if k in r} for r in regions]
        fast.restore_status(state,regions)
        for i in state['dirty']:
            for k in fast.STATUS:regions[i].pop(k,None)
            regions[i].update(current_status[i])
    elif state:
        fast.restore_status(state,regions)
        plans=fast.cached_plans(state,regions,font_path,layout,image.size)
        erase=np.zeros((image.height,image.width),np.uint8)
    else:erase,plans=build_plans(image,regions,scope,layout,font_path,cleanup)
    if incremental:
        incremental['metrics'].update(cache_hit=bool(state),activation=activation,review_reuse=review_reuse,
            rendered_frames=len(plans)-len(removed_ids),restored_frames=len(removed_ids),total_frames=len(regions))
        incremental['metrics']['geometry_seconds']=round(time.perf_counter()-geometry_started,3)
    if (cleanup or {}).get('strength')=='enhanced' and plans:
        safe=np.logical_or.reduce([p['safe']>0 for p in plans])
        erase=cv2.dilate(erase,np.ones((5,5),np.uint8))*safe.astype(np.uint8)
    cache=destination.parent.parent/'cache/mtu-render';cache.mkdir(parents=True,exist_ok=True)
    own=backend is None
    process=backend or MTUProcess(SimpleNamespace(root=cache),root=next((r.get('mtu_root','') for r in regions if r.get('mtu_root')),''))
    try:
        with tempfile.TemporaryDirectory(prefix='render-',dir=cache) as directory:
            temporary=Path(directory);mask=temporary/'erase.png';Image.fromarray(erase).save(mask)
            payload=[];fills=[]
            for plan in plans:
                safe_path=temporary/f"{plan['id']}-safe.png";Image.fromarray(plan['safe']).save(safe_path)
                payload.append({k:v for k,v in plan.items() if k!='safe'}|{'safe_mask':str(safe_path)})
                if plan['fill'] is not None and (not activation or plan['id'] in state['activation_ids']):
                    fills.append({'safe_mask':str(safe_path),'color':plan['fill']})
            clean_key=hashlib.sha256(source.read_bytes()+erase.tobytes()+json.dumps([p['fill'] for p in plans]).encode()+b'lama-large-v2'+
                (state['cleaned_sha256'].encode() if activation else b'')+
                (json.dumps(sorted(removed_ids)).encode() if removed_ids else b'')).hexdigest() if not state or activation else None
            cleaned=Path(state['cleaned']) if state and not activation else cache/(clean_key+'.png')
            clean_started=time.perf_counter();clean_result={}
            if not cleaned.exists() and (not activation or added_plans):clean_result=process.request('clean',path=str(source),mask=str(mask),output=str(cleaned),
                               gpu=getattr(process,'gpu',next((r.get('mtu_gpu',True) for r in regions),True)),fills=fills,local=activation)
            if incremental:incremental['metrics'].update(clean_seconds=round(time.perf_counter()-clean_started,3),
                clean_device=clean_result.get('device','cached'),clean_work_size=clean_result.get('work_size'))
            if activation:
                # Keep the already repaired background of other balloons.
                with Image.open(state['cleaned']) as old:base=np.asarray(old.convert('RGB')).copy()
                if added_plans:
                    with Image.open(cleaned) as fresh:patch=np.asarray(fresh.convert('RGB'))
                    changed=np.logical_or.reduce([p['safe']>0 for p in added_plans])
                    base[changed]=patch[changed]
                for plan in plans:
                    if plan['id'] in removed_ids:base[plan['safe']>0]=np.asarray(image)[plan['safe']>0]
                Image.fromarray(base).save(cleaned)
            bounds=PageBoundaries(image,[r['box'] for r in regions]) if (cleanup or {}).get('strokes') or (manual_selections and not state) else None
            if (cleanup or {}).get('strokes'):
                from .cleanup import repair_background
                with Image.open(cleaned) as base:repaired=repair_background(image,base.convert('RGB'),cleanup,bounds,backend=process)
                cleaned=temporary/'manually-cleaned.png';repaired.save(cleaned)
            rendered=temporary/'translated.png'
            render_started=time.perf_counter()
            if clean_only:
                rendered=cleaned;result={'rendered':[]}
            elif state and removed_ids and all(p['id'] in removed_ids for p in plans):
                # Clearing text needs neither font initialization nor a native
                # renderer. Restore exact original pixels into the old edition.
                with Image.open(incremental['previous']) as previous:pixels=np.asarray(previous.convert('RGB')).copy()
                original=np.asarray(image)
                for plan in plans:pixels[plan['safe']>0]=original[plan['safe']>0]
                Image.fromarray(pixels).save(rendered)
                result={'rendered':[dict(id=p['id'],font_size=0,box=p['rectangle']) for p in plans],'overflow':[]}
            elif state and not plans:
                import shutil
                shutil.copy2(incremental['previous'],rendered);result={'rendered':[]}
            else:result=process.request('render',path=str(cleaned),output=str(rendered),font=font_path,plans=payload,
                                       **({'previous':str(incremental['previous'])} if state else {}))
            if incremental:incremental['metrics']['render_seconds']=round(time.perf_counter()-render_started,3)
            with Image.open(rendered) as im:output=im.convert('RGB')
            if output.size!=image.size:raise ValueError('重排结果尺寸错误，旧译图已保留')
            by_id={plan['id']:plan for plan in plans}
            if not clean_only:
                pixels=np.asarray(output).copy();original=np.asarray(image)
                for info in result.get('overflow',[]):
                    plan=by_id[info['id']]
                    # LaMa has already erased the source. Restore the complete
                    # original safe area of a pending frame, including its ink.
                    pixels[plan['safe']>0]=original[plan['safe']>0]
                    mark_overflow(regions[info['id']],info['message'],info['box'])
                output=Image.fromarray(pixels)
            permitted=np.zeros(erase.shape,bool)
            for plan in plans:permitted|=plan['safe']>0
            if (cleanup or {}).get('strokes'):
                from .cleanup import brush_mask
                permitted|=brush_mask(image,cleanup,bounds)>0
            reference=image
            if state:
                with Image.open(incremental['previous']) as previous:reference=previous.convert('RGB')
            if output.size!=image.size or not np.array_equal(np.asarray(output)[~permitted],np.asarray(reference)[~permitted]):
                raise ValueError('重排结果改变了边框外像素，旧译图已保留')
            for info in result['rendered']:
                r=regions[info['id']];r['render_font_size']=info['font_size'];r['render_box']=info['box']
                clear_overflow(r)
                if info['id'] in removed_ids:
                    r.pop('render_font_size',None);r.pop('render_box',None)
            for selection in (manual_selections or []) if not state else []:
                x,y,w,h=selection['box']
                patch=render_image(image.crop((x,y,x+w,y+h)),selection['regions'],font_path,'all',layout,
                                   boundaries=bounds,boundary_offset=(x,y),cleanup=cleanup,clean_only=clean_only,backend=process)
                if selection.get('recognition_box'):
                    from .layout_area import composite_selection,covered_by_selection
                    previous=[(r,(0,0)) for r in regions if covered_by_selection(r,selection)]
                    output=composite_selection(image,output,patch,selection,previous)
                else:output.paste(patch,(x,y))
            destination.parent.mkdir(parents=True,exist_ok=True)
            target=destination.with_suffix('.tmp.png');output.save(target);target.replace(destination)
            if cacheable:
                if state and not activation:fast.stage_cached_state(incremental,state,regions,destination,font_path,layout)
                else:
                    key=fast.geometry_key(source,regions,scope,cleanup,manual_selections)
                    fast.stage_state(incremental,key,regions,added_plans if activation else plans,cleaned,destination,font_path,layout,image,
                                     previous=state if activation else None,removed=removed_ids)
    finally:
        if own:process.close()
    return destination
