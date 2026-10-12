"""Requested resolution and actual pixel dimensions, independent of credentials."""
import re


def output_size(settings,engine=None,upload=None):
    engine=engine or settings.color_engine
    if engine not in ('openai','nano','klein'):raise ValueError('上色模型无效')
    value=getattr(settings,'color_'+engine+'_size').strip().replace('×','x')
    # Resume older Gemini jobs with their original explicit size class.
    if engine=='nano' and value in ('1K','2K','4K'):return value
    follow=value.lower()=='auto'
    if follow and upload is None:return 'auto'
    if follow:value=f'{upload[0]}x{upload[1]}'
    if not re.fullmatch(r'[1-9]\d{0,3}x[1-9]\d{0,3}',value):
        raise ValueError('请填写有效的生成宽度和高度')
    w,h=map(int,value.split('x'))
    if max(w,h)>8192 or w*h>40_000_000:raise ValueError('生成尺寸单边最多 8192，且不超过 4000 万像素')
    if not follow and (w%16 or h%16):raise ValueError('自定义生成尺寸的宽高须为 16 的倍数；可用范围以模型为准')
    if engine=='openai' and settings.color_openai_interface=='chat':
        raise ValueError('聊天接口无法精确指定生成宽高；请使用图片编辑或图生图接口')
    if engine=='openai' and settings.color_openai_model.startswith('gpt-image-2'):
        if w%16 or h%16 or max(w,h)>3840 or max(w,h)>3*min(w,h) or not 655_360<=w*h<=8_294_400:
            raise ValueError(f'当前 GPT Image 模型的公开尺寸限制不支持 {value}：宽高须为 16 的倍数，单边最多 3840，比例不超过 3:1，总像素为 655360–8294400。请调整上传尺寸或自定义，未自动缩小')
    if engine=='klein' and (w%16 or h%16):
        raise ValueError(f'Klein 无法精确生成 {value}，宽高须为 16 的倍数；请调整上传尺寸或选择自定义')
    if engine=='nano' and upload is not None:nano_image_config(value,settings.color_nano_model)
    return value


def nano_image_config(size,model):
    if size in ('1K','2K','4K'):return {'imageSize':size}
    # Official pixel table: no nearest-size conversion or implicit downscaling.
    base={'1:1':(1024,1024),'2:3':(848,1264),'3:2':(1264,848),
          '3:4':(896,1200),'4:3':(1200,896),'4:5':(928,1152),'5:4':(1152,928),
          '9:16':(768,1376),'16:9':(1376,768),'21:9':(1584,672)}
    classes=(('1K',1),('2K',2),('4K',4))
    if '2.5-flash-image' in model:
        base=dict(zip(base,[(1024,1024),(832,1248),(1248,832),(864,1184),(1184,864),
                            (896,1152),(1152,896),(768,1344),(1344,768),(1536,672)]))
        classes=(('1K',1),)
    elif 'flash' in model or 'nano-banana' in model:
        base.update({'1:4':(512,2048),'4:1':(2048,512),'1:8':(384,3072),'8:1':(3072,384)})
        if 'lite' in model:classes=(('1K',1),)
    for ratio,(w,h) in base.items():
        for label,multiplier in classes:
            if size==f'{w*multiplier}x{h*multiplier}':
                config={'aspectRatio':ratio}
                if '2.5-flash-image' not in model:config['imageSize']=label
                return config
    raise ValueError(f'Gemini 的尺寸档位无法精确请求 {size}；请调整上传尺寸或自定义为支持的尺寸，例如 1024x1024、2048x2048。未自动缩小尺寸')


def upload_dimensions(source,limit,square=False):
    # Same aspect-error rounding as Pillow.thumbnail, without pixel allocation.
    from math import floor,ceil
    width,height=source;content=tuple(source)
    if limit>0 and max(source)>limit:
        aspect=width/height
        if width<=height:
            width=max(1,min(floor(limit*aspect),ceil(limit*aspect),key=lambda n:abs(aspect-n/limit)))
            height=limit
        else:
            height=max(1,min(floor(limit/aspect),ceil(limit/aspect),key=lambda n:0 if n==0 else abs(aspect-limit/n)))
            width=limit
        content=(width,height)
    return ((max(content),)*2 if square else content),content


def size_text(size):
    return f'{size[0]}×{size[1]}' if size else '未记录'


def dimension_trace(source,upload,content,requested,model,saved):
    trace={'source':list(source),'upload_canvas':list(upload) if upload else None,
           'upload_content':list(content) if content else None,'requested':requested,
           'model':list(model),'saved':list(saved),'warning':''}
    if requested not in ('auto','1K','2K','4K'):
        expected=tuple(map(int,requested.split('x')))
        if tuple(model)!=expected:trace['warning']='模型实际返回尺寸与请求尺寸不一致'
    elif requested in ('1K','2K','4K'):
        # Gemini size classes vary with aspect ratio, so compare conservatively.
        minimum={'1K':1000,'2K':2000,'4K':4000}[requested]
        if max(model)<minimum:trace['warning']='模型返回的分辨率低于所选生成档位'
    effective=list(model)
    if upload and content:
        effective=[round(model[i]*content[i]/upload[i]) for i in (0,1)]
    trace['model_content_estimate']=effective
    if any(effective[i]<source[i] for i in (0,1)):
        trace['warning']+=('；' if trace['warning'] else '')+'模型画面分辨率低于来源，保存回来源尺寸不等于恢复原有细节'
    return trace


def trace_text(trace):
    if not trace:return '尚无尺寸记录'
    text=(f"来源 {size_text(trace.get('source'))} → 上传 {size_text(trace.get('upload_canvas'))}"
          f" → 模型返回 {size_text(trace.get('model'))} → 保存 {size_text(trace.get('saved'))}")
    if trace.get('upload_content')!=trace.get('upload_canvas'):
        text+=f"\n上传画布含补白；漫画内容 {size_text(trace.get('upload_content'))}"
    text+='\n请求生成尺寸：'+str(trace.get('requested','auto'))
    if trace.get('warning'):text+='\n'+trace['warning']
    return text
