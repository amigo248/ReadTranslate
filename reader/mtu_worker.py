"""JSON sidecar running inside the reader's owned Python, with no GUI or cloud calls.

MTU's unmodified GPL-3.0 core is bundled locally. This adapter only calls
its public Python dispatch functions. stdout is reserved for our IPC protocol.
"""
from __future__ import annotations
import asyncio,contextlib,gc,json,os,sys,traceback
from pathlib import Path

def device_for(gpu,minimum_mib=2600):
    import torch
    if gpu and torch.cuda.is_available() and torch.cuda.mem_get_info()[0]>=minimum_mib*1048576:return 'cuda'
    return 'cpu'


def prepare_mocr():
    """Require owned local weights; adapt upstream's pre-v5 generation API."""
    root=Path(sys.argv[1])/'models/ocr/manga_ocr'
    for name in ('config.json','preprocessor_config.json','pytorch_model.bin','special_tokens_map.json','tokenizer_config.json','vocab.txt'):
        if not (root/name).is_file():raise ValueError('内置 mocr 模型缺失：'+name+'；请恢复 engines 文件夹。')
    import ssl
    context=ssl._create_default_https_context
    keys=('CURL_CA_BUNDLE','REQUESTS_CA_BUNDLE','HF_HUB_DISABLE_SSL_VERIFY','HF_ENDPOINT','HF_HUB_ENDPOINT')
    previous={key:os.environ.get(key) for key in keys}
    try:
        from manga_translator.ocr import model_manga_ocr
    finally:
        # The upstream module changes TLS defaults on import. Our local-only
        # reader needs none of those mirror/download settings.
        ssl._create_default_https_context=context
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
    if not hasattr(model_manga_ocr.VisionEncoderDecoderModel,'generate'):
        from transformers import GenerationMixin
        class GeneratingVisionEncoderDecoder(model_manga_ocr.VisionEncoderDecoderModel,GenerationMixin):
            pass
        model_manga_ocr.VisionEncoderDecoderModel=GeneratingVisionEncoderDecoder


async def analyze(request):
    ocr_model=request.get('ocr_model','48px')
    if ocr_model not in ('48px','mocr'):raise ValueError('不支持的漫画 OCR 模型：'+str(ocr_model))
    import numpy as np
    import torch
    from PIL import Image
    from manga_translator.config import Config
    from manga_translator.detection import dispatch as detect,unload as unload_detector,detector_cache
    from manga_translator.ocr import dispatch as recognize,unload as unload_ocr
    from manga_translator.textline_merge import dispatch as merge
    if ocr_model=='mocr':prepare_mocr()
    config=Config.model_validate({'detector':{'detection_size':2048,'box_threshold':.5,'text_threshold':.5,
                                            'use_yolo_obb':True,'min_box_area_ratio':0},
                                'ocr':{'ocr':ocr_model,'prob':request.get('ocr_prob',.1),'ignore_bubble':0,'min_text_length':0}})
    with Image.open(request['path']) as source:image=np.asarray(source.convert('RGB'))
    device=device_for(request.get('gpu',True),3200 if ocr_model=='mocr' else 2600);torch.set_num_threads(min(8,os.cpu_count() or 1))
    try:
        if request.get('forced_boxes') is not None or request.get('forced_polygons') is not None:
            from manga_translator.utils import Quadrilateral
            polygons=request.get('forced_polygons',[])+[[[x,y],[x+w,y],[x+w,y+h],[x,y+h]]
                   for x,y,w,h in request.get('forced_boxes',[])]
            lines=[Quadrilateral(np.array(points,np.float32),'',1) for points in polygons]
            mask=None
        else:
            lines,mask,_=await detect(config.detector.detector,image,2048,.5,.5,2.5,device,
                                      use_yolo_obb=True,yolo_obb_conf=.4,min_box_area_ratio=0)
        auxiliary=[q for q in lines if (getattr(q,'det_label',None) or getattr(q,'yolo_label',None))=='other']
        lines=[q for q in lines if q not in auxiliary]
        # White backgrounds are processed before dark backgrounds, even in line OCR.
        def priority(q):
            x,y,w,h=map(int,q.aabb.xywh);crop=image[max(0,y):y+h,max(0,x):x+w]
            return 0 if crop.size and float(np.median(crop))>=180 else 1
        lines.sort(key=priority)
        lines=await recognize(config.ocr.ocr,image,lines,config.ocr,device,runtime_config=config)
        blocks=await merge(lines,image.shape[1],image.shape[0],config,model_assisted_other_textlines=auxiliary)
        if request.get('bubble_output'):
            from manga_translator.utils import detect_bubbles_with_mangalens,build_bubble_mask_from_mangalens_result
            bubbles=detect_bubbles_with_mangalens(image,device=device,verbose=False)
            bubble_mask=build_bubble_mask_from_mangalens_result(bubbles,image.shape[:2],erode_ratio=.02)
            Image.fromarray(bubble_mask).save(request['bubble_output'])
        if request.get('mask_output') and mask is not None:
            Image.fromarray(np.asarray(mask,dtype=np.uint8)).resize((image.shape[1],image.shape[0])).save(request['mask_output'])
        return {'blocks':[b.to_dict() for b in blocks],
                'lines':[{'points':q.pts.tolist(),'text':q.text,'prob':float(q.prob),'font_size':float(q.font_size),
                          'direction':q.direction} for q in lines if q.text.strip()],
                'width':image.shape[1],'height':image.shape[0],'device':device,'ocr_model':ocr_model}
    finally:
        await unload_detector(config.detector.detector)
        for key in list(detector_cache):
            if key!=config.detector.detector:
                module=detector_cache.pop(key)
                if getattr(module,'_loaded',False):await module.unload()
        await unload_ocr(config.ocr.ocr)
        gc.collect()
        if torch.cuda.is_available():torch.cuda.empty_cache()


async def upscale(request):
    from PIL import Image
    from manga_translator.config import Upscaler
    from manga_translator.upscaling import dispatch,unload
    engine=request['engine'];ratio=request['ratio'];tile=request.get('tile',256)
    if engine not in ('esrgan','realcugan','mangajanai') or ratio not in (2,3,4) or tile not in (128,256,400):
        raise ValueError('超分方案、倍率或分块大小无效')
    with Image.open(request['path']) as opened:image=opened.convert('RGB')
    root=Path(sys.argv[1]);kwargs={'tile_size':tile};native_ratio=ratio
    if engine=='realcugan':
        kwargs['model_name']=f'{ratio}x-conservative'
        required=[root/f'models/upscaling/realcugan/up{ratio}x-latest-conservative.pth']
    elif engine=='mangajanai':
        from manga_translator.upscaling.mangajanai import is_color_image
        native_ratio=2 if ratio==2 else 4
        name=(f'{native_ratio}x_IllustrationJaNai_V1_ESRGAN_'+('120k' if native_ratio==2 else '135k')+'.pth') if is_color_image(image) else (f'{native_ratio}x_MangaJaNai_1600p_V1_ESRGAN_'+('90k' if native_ratio==2 else '70k')+'.pth')
        kwargs['model_name']=name
        required=[root/'models/upscaling/mangajanai'/name]
    else:
        kwargs={}
        required=[root/'models/upscaling/esrgan-win/realesrgan-ncnn-vulkan.exe',
                  root/'models/upscaling/esrgan-win/models/realesr-animevideov3-x2.param',
                  root/'models/upscaling/esrgan-win/models/realesr-animevideov3-x2.bin']
    if any(not p.is_file() for p in required):raise ValueError('内置超分组件缺失，请恢复阅译 engines 文件夹；不会使用外部 MTU 或自动下载')
    device=device_for(request.get('gpu',True),1600)
    key=Upscaler(engine)
    try:
        result=(await dispatch(key,[image],native_ratio,device,**kwargs))[0]
        if result.size!=(image.width*native_ratio,image.height*native_ratio):raise ValueError('超分引擎没有输出放大图，请检查驱动和日志')
        if native_ratio!=ratio:result=result.resize((image.width*ratio,image.height*ratio),Image.Resampling.LANCZOS)
        result.save(request['output'])
        return {'width':result.width,'height':result.height,'device':'vulkan' if engine=='esrgan' else device,'native_ratio':native_ratio}
    finally:await unload(key,**kwargs)


async def clean(request):
    import numpy as np
    from PIL import Image
    with Image.open(request['path']) as source:image=np.asarray(source.convert('RGB')).copy()
    with Image.open(request['mask']) as source:mask=np.asarray(source.convert('L')).copy()
    if mask.shape!=image.shape[:2]:raise ValueError('文字蒙版与原图尺寸不一致')
    fills=[]
    for fill in request.get('fills',[]):
        with Image.open(fill['safe_mask']) as source:allowed=np.asarray(source.convert('L'))>0
        fills.append((allowed,fill['color']))
        mask[allowed]=0  # These pixels are replaced by a solid fill afterwards.
    device='none'
    bounds=[0,0,image.shape[1],image.shape[0]]
    if mask.any():
        from manga_translator.config import Inpainter,InpainterConfig
        from manga_translator.inpainting import dispatch,unload
        device=device_for(request.get('gpu',True),3000)
        if request.get('local'):
            ys,xs=np.nonzero(mask);padding=max(128,min(256,round(max(np.ptp(xs),np.ptp(ys))*.35)))
            crop=[max(0,int(xs.min())-padding),max(0,int(ys.min())-padding),
                  min(image.shape[1],int(xs.max())+padding+1),min(image.shape[0],int(ys.max())+padding+1)]
            if (crop[2]-crop[0])*(crop[3]-crop[1])<image.shape[0]*image.shape[1]*.65:bounds=crop
        x,y,right,bottom=bounds;area=image[y:bottom,x:right];area_mask=mask[y:bottom,x:right]
        try:
            result=await dispatch(Inpainter.lama_large,area,area_mask,InpainterConfig(),2048,device)
            area[area_mask>0]=result[area_mask>0]  # Preserve every pixel outside the authorized text mask.
        finally:await unload(Inpainter.lama_large)
    for allowed,color in fills:image[allowed]=color
    Image.fromarray(image).save(request['output'])
    return {'device':device,'work_size':[bounds[2]-bounds[0],bounds[3]-bounds[1]],
            'local':bounds!=[0,0,image.shape[1],image.shape[0]]}


_render_fonts_ready=False


async def render(request):
    global _render_fonts_ready
    import numpy as np
    from PIL import Image
    from manga_translator.config import Config
    from manga_translator.utils import TextBlock
    from manga_translator.rendering import dispatch,text_render
    from manga_translator.rendering.auto_linebreak import _solve_unified_no_br_layout
    from manga_translator.rendering.text_render._fonts import load_font_file
    from PyQt6.QtCore import QChar
    from PyQt6.QtGui import QFontDatabase
    # Qt offscreen does not automatically enumerate Windows fallback fonts when
    # the primary face is loaded by filename. Register Unicode punctuation and
    # CJK fallback faces before any layout measurement (avoids '?' substitutes).
    if not _render_fonts_ready:
        fallbacks=[]
        for font in (Path('C:/Windows/Fonts/seguisym.ttf'),Path(sys.argv[1])/'fonts/Arial-Unicode-Regular.ttf',
                     Path(sys.argv[1])/'fonts/msyh.ttc'):
            if font.is_file():fallbacks.append(load_font_file(str(font)))
    # An installed comic font declares U+2665 but draws a kana-shaped symbol.
    # Give punctuation and hearts a deterministic Unicode fallback in Qt.
        QFontDatabase.setApplicationFallbackFontFamilies(QChar.Script.Script_Common,fallbacks)
        cjk=[load_font_file(str(Path(sys.argv[1])/'fonts'/name)) for name in
             ('simhei.ttf','msyh.ttc','Arial-Unicode-Regular.ttf','NotoSansCJKsc-Regular.otf','NotoSansCJKsc-Bold.otf')
             if (Path(sys.argv[1])/'fonts'/name).is_file()]
        QFontDatabase.setApplicationFallbackFontFamilies(QChar.Script.Script_Han,cjk)
        _render_fonts_ready=True
    with Image.open(request['path']) as source:image=np.asarray(source.convert('RGB')).copy()
    cleaned=image
    if request.get('previous'):
        with Image.open(request['previous']) as source:image=np.asarray(source.convert('RGB')).copy()
        if image.shape!=cleaned.shape:raise ValueError('局部更新底图尺寸错误')
    metadata=[];overflow=[]
    for item in request['plans']:
        font=item.get('font') or request['font']
        x,y,w,h=item['rectangle']
        with Image.open(item['safe_mask']) as source:safe=np.asarray(source.convert('L'))>0
        if safe.shape!=image.shape[:2]:raise ValueError('排版蒙版尺寸错误')
        if request.get('previous'):image[safe]=cleaned[safe]
        if not item['translation'].strip():
            metadata.append({'id':item['id'],'font_size':0,'box':[x,y,w,h]});continue
        # Render each candidate inside a padded local canvas. The padding remains
        # outside the safe mask, so clipped/overflowing ink is still rejected.
        if request.get('full_canvas'):
            left,top,right,bottom=0,0,image.shape[1],image.shape[0]
        else:
            ys,xs=np.nonzero(safe)
            pad=max(32,int(item['size'])*2)
            # The native compositor uses Python's round-to-even for half-pixel
            # anchors. Even crop origins preserve that rounding in page space.
            left,top=(max(0,int(xs.min())-pad)//2)*2,(max(0,int(ys.min())-pad)//2)*2
            right,bottom=min(image.shape[1],int(xs.max())+pad+1),min(image.shape[0],int(ys.max())+pad+1)
        local=image[top:bottom,left:right].copy();local_safe=safe[top:bottom,left:right]
        a,b=x-left,y-top;points=[[[a,b],[a+w,b],[a+w,b+h],[a,b+h]]]
        succeeded=False
        for size in range(max(8,int(item['size'])),7,-1):
            config=Config.model_validate({'render':{'font_family':font,'layout_mode':'strict',
                   'font_size_minimum':0,'font_size':size,'center_text_in_bubble':True,
                   'alignment':'center','stroke_width':.035,'direction':item['direction']}})
            block=TextBlock(points,texts=[item['source']],translation=item['translation'],font_size=size,
                    direction='v' if item['direction']=='vertical' else 'h',alignment='center',
                    fg_color=tuple(item['color']),bg_color=tuple(item['background']),
                    font_family=font,target_lang='CHS',default_stroke_width=.035)
            # Fit translated text to every safe rectangle, independent of the
            # original OCR line count. Do this again for each candidate size;
            # shrinking alone must not preserve an overflowing single column.
            text_render.set_font(font)
            import re
            paragraphs=re.split(r'\[BR\]|【BR】|<br>|\n',item['translation'],flags=re.IGNORECASE)
            lines=[]
            for paragraph in paragraphs:
                wrapped=_solve_unified_no_br_layout(paragraph,item['direction']!='vertical',
                    size,max(1,w-4),max(1,h-4),8,1.0,1.0,config,'CHS',size)
                lines.extend(wrapped.split('[BR]'))
            block.translation_rich={'format':'richtext.v1','blocks':[{'type':'paragraph','inlines':
                [{'type':'text','text':line,'style':{'bold':item.get('weight')=='bold'}}]} for line in lines]}
            alpha=np.zeros(local.shape[:2],np.uint8)
            result=await dispatch(local.copy(),[block],config,original_img=None,skip_text_replacements=False,render_alpha=alpha)
            changed=np.any(result!=local,axis=2)
            # Never clip overflowing Chinese. Retry smaller until the entire ink fits.
            if block.font_size>=8 and alpha.any() and not np.any(changed&~local_safe) and not np.any((alpha>0)&~local_safe):
                image[top:bottom,left:right]=result;metadata.append({'id':item['id'],'font_size':int(block.font_size),
                    'box':[x,y,w,h],'line_count':len(lines)})
                succeeded=True;break
        if not succeeded:
            overflow.append({'id':item['id'],'box':[x,y,w,h],'message':
                f'安全区域 {w}×{h} px：缩小至 8 px 后译文仍放不下。中文已保存，请在校对界面缩短译文或调整排版后重新生成。'})
    Image.fromarray(image).save(request['output'])
    return {'rendered':metadata,'overflow':overflow}


async def info(request):
    import torch
    return {'python':sys.version.split()[0],'torch':torch.__version__,'cuda':torch.cuda.is_available(),
            'executable':sys.executable,'prefix':sys.prefix,'torch_path':torch.__file__,
            'engine_root':sys.argv[1],
            'ocr_model':request.get('ocr_model','48px'),
            'mocr_model_path':str(Path(sys.argv[1])/'models/ocr/manga_ocr'),
            'free_mib':torch.cuda.mem_get_info()[0]//1048576 if torch.cuda.is_available() else 0}


def serve():
    root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root))
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    for line in sys.stdin:
        try:
            request=json.loads(line)
            if request.get('quit'):break
            with contextlib.redirect_stdout(sys.stderr):
                result=asyncio.run({'analyze':analyze,'clean':clean,'render':render,'info':info,'upscale':upscale}[request['action']](request))
            print(json.dumps({'ok':True,**result},ensure_ascii=True,default=lambda x:x.tolist()),flush=True)
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            print(json.dumps({'ok':False,'error':str(error)},ensure_ascii=True),flush=True)


if __name__=='__main__':serve()
