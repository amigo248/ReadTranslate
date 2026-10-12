"""Standalone image API transport and diagnostics, informed by MTU's workflows.

No API SDK, external MTU installation, automatic paid retry, or secret in a job.
"""
import base64
import io
import ipaddress
import os
import re
import socket
import time
from urllib.parse import quote, urlparse

import httpx
from PIL import Image, ImageOps

DEFAULT_PROMPT = (
    'Colorize image 1, a black-and-white manga page. Add natural anime colors only. '
    'Preserve every face, pose, line, screentone, panel, margin and all lettering. '
    'Leave speech balloon interiors and gutters unchanged. Do not invent objects or text. '
    'Keep any white padding unchanged. Image 1 is the only target page. '
    'All other attached images are references for palette, characters, materials and lighting; '
    'never copy their composition into image 1.'
)
MAX_IMAGE = 48 * 1024 * 1024
MAX_BASE64 = 64 * 1024 * 1024
DATA_IMAGE = re.compile(r'data:image/(?:png|jpe?g|webp);base64,([A-Za-z0-9+/=\r\n]+)', re.I)
MARKDOWN_IMAGE = re.compile(r'!\[[^\]]*\]\((https://[^\s)]+)\)')


def sanitize(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[密钥已隐藏]')
    text = re.sub(r'data:image/[^\s]+', '[图片数据已隐藏]', text, flags=re.I)
    text = re.sub(r'(?i)(Bearer[ \t]+)[A-Za-z0-9._~+/-]{3,}', r'\1[已隐藏]', text)
    text = re.sub(r'(?i)((?:api[_ -]?key|x-goog-api-key|token|secret|authorization)[ \t\"]*[:=][ \t\"]*)[^\s,\"<>]+',
                  r'\1[已隐藏]', text)
    text = re.sub(r'\b(?:sk-[\w-]+|AIza[\w-]+)\b', '[密钥已隐藏]', text)
    text = re.sub(r'(https?://[^\s?\"<>]+)\?[^\s\"<>]*', r'\1?[参数已隐藏]', text)
    text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)
    return text[:1800]


def validate_url(value):
    value = value.strip()
    p = urlparse(value)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('请填写不含密钥、参数或账号的 HTTP/HTTPS 接口地址')
    if p.scheme == 'http' and p.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('非本机接口必须使用 HTTPS')
    return value.rstrip('/')


def api_base(value, engine):
    value = validate_url(value)
    p = urlparse(value)
    path = p.path.rstrip('/')
    if any(s in path for s in (':generateContent', '/images/', '/chat/completions')):
        raise ValueError('请填写 API 基础地址，不要填写完整的生成接口路径')
    if engine == 'nano':
        if path.endswith('/models'):
            value = value[:-7]
        elif not path:
            value += '/v1beta'
        if urlparse(value).path.endswith('/v1'):
            raise ValueError('Gemini 原生接口请使用 /v1beta；OpenAI 兼容接口请切换到 OpenAI 上色')
    else:
        if path.endswith('/models'):
            value = value[:-7]
        elif not path:
            value += '/v1'
        if urlparse(value).path.endswith('/v1beta'):
            raise ValueError('OpenAI 兼容接口请使用 /v1，Gemini 原生接口请切换上色引擎')
    return value


def system_proxy(url):
    p = urlparse(url)
    if p.hostname in ('localhost', '127.0.0.1', '::1'):
        return None
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
                enabled = winreg.QueryValueEx(key, 'ProxyEnable')[0]
                server = winreg.QueryValueEx(key, 'ProxyServer')[0]
            if enabled and server:
                entries = dict(part.split('=', 1) for part in server.split(';') if '=' in part)
                address = entries.get(p.scheme) if entries else server
                if address:
                    return address if '://' in address else 'http://' + address
        except (OSError, ValueError):
            pass
    return None


def make_client(settings, base, transport=None, timeout=None):
    use_proxy = settings.color_network == 'system'
    return httpx.Client(timeout=httpx.Timeout(timeout or settings.color_timeout, connect=20),
                        trust_env=use_proxy, proxy=system_proxy(base) if use_proxy and transport is None else None,
                        transport=transport, follow_redirects=False)


class ColorAPIError(ValueError):
    pass


def response_json(response, label, secrets=()):
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not response.is_success or isinstance(payload, dict) and payload.get('error'):
        error = payload.get('error') if isinstance(payload, dict) else None
        if isinstance(error, dict):
            reason = error.get('message') or error.get('detail') or '服务商未提供原因'
            code = error.get('code') or error.get('type') or error.get('status') or ''
            request_id = error.get('request_id') or error.get('requestId') or ''
        else:
            reason = error or (payload.get('message') if isinstance(payload, dict) else None) or response.text[:1800]
            code = ''
            request_id = ''
        if isinstance(payload, dict):
            request_id = request_id or payload.get('request_id') or payload.get('requestId')
        request_id = request_id or response.headers.get('x-request-id') or response.headers.get('request-id')
        status = response.status_code
        suggestion = {
            400:'检查模型、请求参数和接口类型。', 401:'密钥无效或已过期，请检查当前上色引擎的密钥。',
            402:'检查余额或额度。', 403:'检查密钥权限、模型权限或服务限制。',
            404:'检查基础地址、模型名称及接口类型。', 405:'服务商不支持此接口类型。',
            429:'服务商限流或额度不足，请稍后手动重试。',
        }.get(status, '上游或渠道不可用，请稍后重试或更换渠道。' if status >= 500 else '检查服务商响应。')
        detail = f'{label}失败（HTTP {status}）\n原因：{reason or "服务商未提供原因"}'
        if code:
            detail += f'\n错误码：{code}'
        if request_id:
            detail += f'\n请求 ID：{request_id}'
        detail += '\n' + suggestion
        raise ColorAPIError(sanitize(detail, secrets)) from None
    if not isinstance(payload, dict):
        raise ColorAPIError(f'{label}没有返回有效的 JSON 对象') from None
    return payload


def network_error(error, label):
    if isinstance(error, httpx.TimeoutException):
        reason = '连接或读取超时'
    elif isinstance(error, httpx.ProxyError):
        reason = '代理连接失败，请检查系统代理或切换为直连'
    elif 'ssl' in str(error).lower() or 'tls' in str(error).lower():
        reason = 'TLS/SSL 连接失败，请检查证书、网络和代理；不要关闭证书校验'
    else:
        reason = '网络连接失败，请检查域名、服务地址、网络和代理'
    return ColorAPIError(f'{label}：{reason}。未自动重试；已提交的请求可能计费，请先查看服务商记录。')


def decode_image(raw):
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_IMAGE:
            raise ValueError()
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError()
            return ImageOps.exif_transpose(image).convert('RGB').copy()
    except (ValueError, OSError, TypeError, Image.DecompressionBombError):
        raise ColorAPIError('返回的图片无效或过大，旧上色版本已保留') from None


def encoded_image(value):
    if not isinstance(value, str) or len(value) > MAX_BASE64:
        raise ColorAPIError('返回的图片无效或数据过大，旧上色版本已保留') from None
    try:
        return decode_image(base64.b64decode(value.replace('\r', '').replace('\n', ''), validate=True))
    except ValueError:
        raise ColorAPIError('返回的图片无效（编码错误），旧上色版本已保留') from None


def png_bytes(image, max_side):
    image = ImageOps.exif_transpose(image).convert('RGBA')
    rgb = Image.new('RGB', image.size, 'white');rgb.paste(image, mask=image.getchannel('A'))
    if max_side > 0:
        rgb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO();rgb.save(buffer, format='PNG')
    return buffer.getvalue(), rgb.size


def prepare_page(image, settings):
    raw, size = png_bytes(image, settings.color_max_side)
    page = decode_image(raw)
    if not settings.color_square:
        return raw, None
    w, h = size;side = max(size);x = (side-w)//2;y = (side-h)//2
    square = Image.new('RGB', (side, side), 'white');square.paste(page, (x,y))
    buffer = io.BytesIO();square.save(buffer, format='PNG')
    return buffer.getvalue(), (x,y,w,h,side)


def restore_page(image, info, size):
    from .color_geometry import normalize_result
    return normalize_result(image,size,info)[0]


class ImageAPI:
    def __init__(self, settings, engine, key, cancel=lambda:False, transport=None):
        self.settings,self.engine,self.key,self.cancel = settings,engine,key,cancel
        self.base = api_base(getattr(settings, 'color_'+('nano' if engine=='nano' else 'openai')+'_url'), engine)
        self.model = getattr(settings, 'color_'+('nano' if engine=='nano' else 'openai')+'_model').strip()
        if engine == 'nano':
            self.model = self.model.removeprefix('models/')
        self.client = make_client(settings, self.base, transport)
        self.download_transport = transport
        self.last_interface = ''
        self.last_usage = {}
        self.denoiser = None
        self.last_raw_result = None
        self.last_geometry = None
        self.last_sizes = None

    def credentials(self):
        if not self.key or not self.key.isascii() or any(ord(c)<33 or ord(c)>126 for c in self.key):
            raise ColorAPIError('请填写当前上色引擎的有效密钥（不能包含空格、换行或非 ASCII 字符）')
        return {'x-goog-api-key':self.key} if self.engine=='nano' else {'Authorization':'Bearer '+self.key}

    def check_cancel(self):
        if self.cancel():
            raise InterruptedError('上色/API 检测已取消；已提交请求可能仍会计费')

    def request(self, method, path, **kwargs):
        self.check_cancel()
        try:
            return self.client.request(method, self.base+path, headers=self.credentials(), **kwargs)
        except httpx.HTTPError as error:
            raise network_error(error, '上色 API') from None

    def get_models(self):
        models = set();token = '';seen = set()
        for _ in range(30):
            params = {'pageToken':token} if token else {}
            payload = response_json(self.request('GET', '/models', params=params, timeout=60), '获取上色模型', (self.key,))
            items = payload.get('models') if self.engine=='nano' else payload.get('data')
            if not isinstance(items, list):
                raise ColorAPIError('服务商返回的模型列表格式不正确')
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get('name') or item.get('id') if self.engine=='nano' else item.get('id')
                if isinstance(name, str) and name.strip():
                    models.add(name.removeprefix('models/') if self.engine=='nano' else name)
            token = payload.get('nextPageToken', '') if self.engine=='nano' else ''
            if not token:
                return sorted(models, reverse=True)
            if not isinstance(token, str) or token in seen:
                raise ColorAPIError('模型列表分页异常，请检查服务商')
            seen.add(token)
        raise ColorAPIError('模型列表分页过多，请检查服务商')

    def download_image(self, url):
        # Never send API authentication, cookies or environment credentials to a CDN.
        for _ in range(4):
            p = urlparse(url)
            if p.scheme!='https' or not p.hostname or p.username or p.password or p.hostname.lower()=='localhost':
                raise ColorAPIError('图片下载地址必须是公开 HTTPS 地址')
            try:
                if not ipaddress.ip_address(p.hostname).is_global:
                    raise ColorAPIError('不允许从本机或内网地址下载服务商图片')
            except ValueError as error:
                if isinstance(error, ColorAPIError):
                    raise
            if self.download_transport is None:
                try:addresses=socket.getaddrinfo(p.hostname,p.port or 443,type=socket.SOCK_STREAM)
                except OSError:raise ColorAPIError('上色已完成，但图片下载域名解析失败；请检查网络，勿重复上色以免重复计费') from None
                if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                    raise ColorAPIError('服务商图片下载域名指向本机或内网，已停止下载')
            self.check_cancel()
            try:
                with make_client(self.settings, url, self.download_transport, timeout=120) as client:
                    with client.stream('GET', url) as response:
                        if response.is_redirect:
                            url = str(response.url.join(response.headers.get('location', '')))
                            continue
                        if not response.is_success:
                            raise ColorAPIError(f'上色已返回图片地址，但下载失败（HTTP {response.status_code}）；请勿重复上色以免重复计费')
                        try:length=int(response.headers.get('content-length','0'))
                        except ValueError:raise ColorAPIError('图片下载返回了无效的长度信息') from None
                        if length > MAX_IMAGE:
                            raise ColorAPIError('服务商图片超过下载大小限制')
                        content = bytearray()
                        for chunk in response.iter_bytes():
                            self.check_cancel();content.extend(chunk)
                            if len(content)>MAX_IMAGE:
                                raise ColorAPIError('服务商图片超过下载大小限制')
                        return decode_image(bytes(content))
            except httpx.HTTPError as error:
                raise network_error(error, '上色结果下载') from None
        raise ColorAPIError('图片下载重定向次数过多')

    def image_value(self, value):
        if isinstance(value, dict):
            value = value.get('url') or value.get('data')
        if not isinstance(value, str):
            return None
        match = DATA_IMAGE.search(value)
        if match:
            return encoded_image(match.group(1))
        if value.startswith('https://'):
            return self.download_image(value)
        return None

    def extract_image(self, payload):
        for item in payload.get('data', []) if isinstance(payload.get('data'), list) else []:
            if isinstance(item, dict):
                if item.get('b64_json'):
                    return encoded_image(item['b64_json'])
                if item.get('url'):
                    return self.image_value(item['url'])
        parts = []
        texts = [];reasons = []
        feedback = payload.get('promptFeedback') or {}
        if not isinstance(feedback,dict):feedback={}
        if feedback.get('blockReason'):
            reasons.append('服务商拦截原因：'+str(feedback['blockReason']))
        for candidate in payload.get('candidates', []) if isinstance(payload.get('candidates'),list) else []:
            if not isinstance(candidate,dict):continue
            content=candidate.get('content') or {}
            if isinstance(content,dict):parts.extend(content.get('parts') or [])
            if candidate.get('finishReason'):
                reasons.append('结束原因：'+str(candidate['finishReason']))
        for choice in payload.get('choices', []) if isinstance(payload.get('choices'),list) else []:
            if not isinstance(choice,dict):continue
            message = choice.get('message') or {}
            if not isinstance(message,dict):continue
            content = message.get('content')
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                parts.extend(content)
            parts.extend(message.get('images') or [])
            if message.get('refusal'):
                reasons.append(str(message['refusal']))
            if choice.get('finish_reason'):
                reasons.append('结束原因：'+str(choice['finish_reason']))
        for part in parts:
            if not isinstance(part, dict) or part.get('thought'):
                continue
            inline = part.get('inlineData') or part.get('inline_data')
            if isinstance(inline, dict) and inline.get('data') and str(inline.get('mimeType', inline.get('mime_type',''))).startswith('image/'):
                return encoded_image(inline['data'])
            for field in ('image_url', 'url'):
                if part.get(field):
                    image = self.image_value(part[field])
                    if image:
                        return image
            if isinstance(part.get('text'), str):
                texts.append(part['text'])
        for text in texts:
            match = DATA_IMAGE.search(text)
            if match:
                return encoded_image(match.group(1))
            match = MARKDOWN_IMAGE.search(text)
            if match:
                return self.download_image(match.group(1))
        preview = sanitize('；'.join(reasons+texts)[:600], (self.key,))
        raise ColorAPIError('请求成功但没有返回图片，旧上色版本已保留。'+
                            ('\n'+preview if preview else '\n请确认所选模型支持图片输出，上色 API 不等同于文字或识图 API。'))

    def colorize(self, source, reference=None, history=(), on_usage=lambda *_:None):
        self.last_raw_result=None;self.last_geometry=None;self.last_sizes=None
        self.check_cancel()
        if not self.model or len(self.model)>256 or any(ord(c)<32 for c in self.model):
            raise ColorAPIError('请填写有效的上色模型名称')
        from .color_denoise import prepare_input
        from .color_sizes import output_size,dimension_trace,nano_image_config
        output_size(self.settings,self.engine)
        raw, info = prepare_page(prepare_input(self,source), self.settings)
        with Image.open(io.BytesIO(raw)) as uploaded:upload_size=uploaded.size
        content_size=tuple(info[2:4]) if info else upload_size
        requested=output_size(self.settings,self.engine,upload_size)
        images = [raw]
        if reference is not None:
            images.append(png_bytes(reference, self.settings.color_max_side)[0])
        images.extend(png_bytes(image, self.settings.color_max_side)[0] for image in history)
        prompt = self.settings.color_prompt.strip() or DEFAULT_PROMPT
        if len(images)>1:prompt += '\nImage 1 is the only target. Image 2 and later are color references only; preserve image 1 composition and white padding.'
        if history:
            prompt += '\nPreviously colorized pages are attached after the optional reference. Keep cross-page colors consistent.'
        if self.engine=='nano':
            parts = [{'text':prompt}]+[{'inlineData':{'mimeType':'image/png','data':base64.b64encode(raw).decode('ascii')}} for raw in images]
            payload = {'contents':[{'role':'user','parts':parts}], 'generationConfig':{'responseModalities':['TEXT','IMAGE']}}
            payload['generationConfig']['imageConfig']=nano_image_config(requested,self.model)
            response = self.request('POST', '/models/'+quote(self.model, safe='')+':generateContent', json=payload)
            self.last_interface = 'Gemini generateContent'
        else:
            selected = self.settings.color_openai_interface
            interfaces = ['edits','generations','chat'] if selected=='auto' else [selected]
            if selected=='auto' and urlparse(self.base).hostname=='openrouter.ai':
                interfaces = ['chat','edits','generations']
            for interface in interfaces:
                if interface=='edits':
                    data = {'model':self.model, 'prompt':prompt, 'n':'1'}
                    if self.model.startswith('gpt-image'):
                        data.update(quality=self.settings.color_openai_quality, size='auto', output_format='png', background='opaque')
                        if self.model in ('gpt-image-1','gpt-image-1.5'):
                            data['input_fidelity'] = 'high'
                        field = 'image[]'
                    else:
                        data['response_format'] = 'b64_json';field = 'image[]' if len(images)>1 else 'image'
                    if requested!='auto':data['size']=requested
                    files = [(field, (f'image-{i+1}.png',raw,'image/png')) for i,raw in enumerate(images)]
                    response = self.request('POST', '/images/edits', data=data, files=files)
                elif interface=='generations':
                    values = ['data:image/png;base64,'+base64.b64encode(raw).decode('ascii') for raw in images]
                    # An image-to-image generation request, never a text-only redraw.
                    payload={'model':self.model, 'prompt':prompt,
                        'image':values[0] if len(values)==1 else values, 'n':1, 'response_format':'b64_json'}
                    if requested!='auto':
                        payload['size']=requested
                        if self.model.startswith('gpt-image'):payload['quality']=self.settings.color_openai_quality
                    response = self.request('POST', '/images/generations', json=payload)
                elif interface=='chat':
                    if requested!='auto':
                        raise ColorAPIError('图片接口不支持当前请求，未切换到无法指定尺寸的聊天接口；请调整图片接口类型')
                    content = [{'type':'text','text':prompt}]+[{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(raw).decode('ascii')}} for raw in images]
                    payload = {'model':self.model,'messages':[{'role':'user','content':content}], 'stream':False}
                    if urlparse(self.base).hostname=='openrouter.ai':
                        payload.update(modalities=['image','text'], max_tokens=2048)
                    response = self.request('POST', '/chat/completions', json=payload)
                else:
                    raise ColorAPIError('请选择有效的 OpenAI 图片接口类型')
                self.last_interface = interface
                if selected=='auto' and response.status_code in (404,405,501) and interface!=interfaces[-1]:
                    continue
                break
        result = response_json(response, '上色', (self.key,))
        self.last_usage = result.get('usageMetadata' if self.engine=='nano' else 'usage') or {}
        if not isinstance(self.last_usage, dict):
            self.last_usage = {}
        on_usage(self.model, self.last_usage)
        image = self.extract_image(result)
        # Retain the decoded provider response before any crop/normalization.
        from .color_geometry import normalize_result
        self.last_raw_result=image.copy()
        output,self.last_geometry=normalize_result(image,source.size,info,source)
        self.last_sizes=dimension_trace(source.size,upload_size,content_size,requested,image.size,output.size)
        return output

    def test_connection(self):
        start = time.monotonic();models = self.get_models()
        found = self.model in models
        return {'message':f'连接与鉴权成功，获取 {len(models)} 个模型。'+
                ('当前模型在列表中。' if found else '当前模型未在列表中，请检查或手动填写服务商模型别名。')+
                '\n此测试未生成图片；模型列表不能保证实际上色成功。',
                'elapsed':time.monotonic()-start,'models':models,'model_listed':found}

    def close(self):
        self.denoiser = None
        self.client.close()
