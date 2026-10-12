"""Image-only providers. Local workflows share the independent API adapter."""
import io,time,uuid
import httpx
from PIL import Image
from .config import get_color_api_key,get_openai_color_api_key
from .color_api import ImageAPI, DEFAULT_PROMPT as PROMPT, png_bytes, validate_url, response_json


class CloudColor:
    engine = ''
    def __init__(self,settings,library,book,cancel,transport=None,key=None):
        self.settings,self.library,self.book,self.cancel = settings,library,book,cancel
        if key is None:
            key = get_color_api_key() if self.engine=='nano' else get_openai_color_api_key()
        self.api = ImageAPI(settings,self.engine,key,cancel,transport)
        self.client = self.api.client
        self.history = []

    def colorize(self,source,reference=None,seed=0):
        def usage(model,values):
            if self.library is None:return
            nano = self.engine=='nano'
            self.library.record_usage(self.book,'上色 / '+('' if nano else 'OpenAI / ')+model,
                values.get('promptTokenCount' if nano else 'input_tokens',values.get('prompt_tokens',0)),
                values.get('candidatesTokenCount' if nano else 'output_tokens',values.get('completion_tokens',0)),
                max(0,self.settings.color_nano_price if nano else self.settings.color_openai_price))
        return self.api.colorize(source,reference,self.history,on_usage=usage)

    def close(self):self.api.close()

    @property
    def last_raw_result(self):return self.api.last_raw_result

    @property
    def last_geometry(self):return self.api.last_geometry

    @property
    def last_sizes(self):return self.api.last_sizes


class NanoBanana(CloudColor):
    engine = 'nano'


class OpenAIColor(CloudColor):
    engine = 'openai'


def klein_workflow(settings, images, size, seed):
    width,height = (max(16, round(n/16)*16) for n in size)
    workflow = {}
    def node(key, kind, **inputs): workflow[str(key)] = {'class_type':kind, 'inputs':inputs}
    node(1,'UNETLoader',unet_name=settings.color_klein_model,weight_dtype='default')
    node(2,'CLIPLoader',clip_name=settings.color_klein_encoder,type='flux2',device='default')
    node(3,'VAELoader',vae_name=settings.color_klein_vae)
    node(4,'CLIPTextEncode',clip=['2',0],text=settings.color_prompt.strip() or PROMPT)
    node(5,'ConditioningZeroOut',conditioning=['4',0])
    positive,negative = ['4',0],['5',0]
    for i,name in enumerate(images):
        k = 20+i*4
        node(k,'LoadImage',image=name)
        node(k+1,'VAEEncode',pixels=[str(k),0],vae=['3',0])
        node(k+2,'ReferenceLatent',conditioning=positive,latent=[str(k+1),0])
        node(k+3,'ReferenceLatent',conditioning=negative,latent=[str(k+1),0])
        positive,negative = [str(k+2),0],[str(k+3),0]
    node(6,'EmptyFlux2LatentImage',width=width,height=height,batch_size=1)
    node(7,'Flux2Scheduler',steps=4,width=width,height=height)
    node(8,'RandomNoise',noise_seed=seed)
    node(9,'KSamplerSelect',sampler_name='euler')
    node(10,'CFGGuider',model=['1',0],positive=positive,negative=negative,cfg=1.0)
    node(11,'SamplerCustomAdvanced',noise=['8',0],guider=['10',0],sampler=['9',0],sigmas=['7',0],latent_image=['6',0])
    node(12,'VAEDecode',samples=['11',0],vae=['3',0])
    node(13,'SaveImage',images=['12',0],filename_prefix='Yueyi-color/'+uuid.uuid4().hex)
    return workflow


class Klein:
    def __init__(self, settings, library, book, cancel, transport=None):
        self.settings,self.cancel = settings,cancel
        self.base = validate_url(settings.color_comfy_url)
        self.client = httpx.Client(base_url=self.base+'/',timeout=60,transport=transport,trust_env=False)
        self.prompt_id = None
        self.checked=False
        self.denoiser=None
        self.last_sizes=None

    def preflight(self):
        try:
            info = response_json(self.client.get('object_info'),'Klein')
        except httpx.HTTPError as error:
            raise ValueError('无法连接 Klein 4B：请先启动 ComfyUI，并在上色设置中填写服务地址') from error
        workflow = klein_workflow(self.settings,['page.png'],(512,512),0)
        missing = {n['class_type'] for n in workflow.values()}-set(info)
        if missing: raise ValueError('ComfyUI 缺少 Klein 节点，请更新 ComfyUI：'+', '.join(sorted(missing)))
        for kind,field,value in [('UNETLoader','unet_name',self.settings.color_klein_model),
                                 ('CLIPLoader','clip_name',self.settings.color_klein_encoder),
                                 ('VAELoader','vae_name',self.settings.color_klein_vae)]:
            choices = info[kind]['input']['required'][field][0]
            if value not in choices: raise ValueError('ComfyUI 未找到模型文件：'+value)
        self.checked=True

    def colorize(self, source, reference=None, seed=0):
        from .color_denoise import prepare_input
        from .color_sizes import dimension_trace,output_size,upload_dimensions
        original_size=source.size;self.last_sizes=None
        canvas,_=upload_dimensions(source.size,self.settings.color_max_side)
        requested=output_size(self.settings,'klein',canvas)
        generation_size=tuple(map(int,requested.split('x')))
        source=prepare_input(self,source)
        if not self.checked:self.preflight()
        names=[];size=None
        for i,image in enumerate((source,reference)):
            if image is None: continue
            if self.cancel(): raise InterruptedError('上色已暂停')
            data,shape = png_bytes(image,self.settings.color_max_side if i==0 else min(1024,self.settings.color_max_side))
            if i==0:size=shape
            upload = response_json(self.client.post('upload/image',files={'image':('yueyi-'+uuid.uuid4().hex+'.png',data,'image/png')},
                                                   data={'type':'input','overwrite':'false'}),'Klein')
            names.append((upload.get('subfolder','')+'/' if upload.get('subfolder') else '')+upload['name'])
        response = response_json(self.client.post('prompt',json={'prompt':klein_workflow(self.settings,names,generation_size,seed),
                                                                'client_id':'yueyi-'+uuid.uuid4().hex}),'Klein')
        self.prompt_id = response.get('prompt_id')
        if not self.prompt_id or response.get('node_errors'): raise ValueError('Klein 工作流未通过验证，请检查模型和 ComfyUI 版本')
        deadline = time.monotonic()+1800
        while time.monotonic()<deadline:
            if self.cancel():
                self.cancel_own()
                raise InterruptedError('上色已暂停；完成的页面已保存')
            history = response_json(self.client.get('history/'+self.prompt_id),'Klein').get(self.prompt_id)
            if history:
                if history.get('status',{}).get('status_str')=='error':
                    raise ValueError('Klein 上色失败；请检查 ComfyUI 日志和显存占用，旧版本已保留')
                for output in history.get('outputs',{}).values():
                    for item in output.get('images',[]):
                        raw = self.client.get('view',params={k:item.get(k,'') for k in ('filename','subfolder','type')})
                        if not raw.is_success: raise ValueError('无法读取 Klein 上色结果')
                        with Image.open(io.BytesIO(raw.content)) as image:result=image.convert('RGB').copy()
                        self.last_sizes=dimension_trace(original_size,size,size,requested,result.size,original_size)
                        return result
                if history.get('status',{}).get('completed'): raise ValueError('Klein 未返回上色图片')
            time.sleep(.4)
        self.cancel_own()
        raise ValueError('Klein 上色超时；已完成页面保留，可从任务记录继续')

    def cancel_own(self):
        if not self.prompt_id:return
        try:
            queue = response_json(self.client.get('queue'),'Klein')
            if any(item[1]==self.prompt_id for item in queue.get('queue_running',[])):
                self.client.post('interrupt',json={})
            self.client.post('queue',json={'delete':[self.prompt_id]})
        except (httpx.HTTPError,ValueError): pass

    def close(self):
        self.denoiser=None
        self.client.close()


def create_backend(settings,library,book,cancel):
    if settings.color_engine=='klein':return Klein(settings,library,book,cancel)
    if settings.color_engine=='nano':return NanoBanana(settings,library,book,cancel)
    if settings.color_engine=='openai':return OpenAIColor(settings,library,book,cancel)
    raise ValueError('请选择 Klein 4B、Nano Banana 或 OpenAI')
