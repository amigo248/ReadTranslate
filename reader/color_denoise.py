"""Owned FFDNet input denoising, matching MTU's manga-colorization-v2 stage.

Architecture adapted from Matias Tassano's FFDNet (2018), GPL-3.0-or-later.
See assets/color-denoise for upstream source, license and verified RGB weights.
"""
import hashlib
import contextlib
import threading
import numpy as np
from PIL import Image
from .config import app_root

WEIGHT_HASH = '0fe98bfd2ac870b15f360661b1c4789eecefc6dc2e4462842a0dd15e149a0433'
_CPU_LOCK=threading.Lock()


@contextlib.contextmanager
def inference_threads(device):
    import torch
    if device!='cpu':
        yield;return
    # Very high host thread counts make this small network much slower.
    # Restore the host's choice when the bounded local inference is finished.
    with _CPU_LOCK:
        previous=torch.get_num_threads()
        try:
            torch.set_num_threads(min(previous,4));yield
        finally:torch.set_num_threads(previous)


def weight_path():
    root=app_root()
    for path in (root/'assets/color-denoise/net_rgb.pth',root/'_internal/assets/color-denoise/net_rgb.pth'):
        if path.is_file():return path
    raise ValueError('上色降噪模型缺失，请修复阅译安装，或将降噪强度设为关闭')


def validate_sigma(value):
    if type(value) is not int or not -1<=value<=255:
        raise ValueError('上色降噪强度必须是 -1（关闭）或 0–255 的整数')
    return value


def build_model():
    # Lazy import: disabled denoising never loads PyTorch or a model.
    import torch
    from torch import nn
    class Middle(nn.Module):
        def __init__(self):
            super().__init__()
            layers=[nn.Conv2d(15,96,3,padding=1,bias=False),nn.ReLU(inplace=True)]
            for _ in range(10):
                layers.extend([nn.Conv2d(96,96,3,padding=1,bias=False),nn.BatchNorm2d(96),nn.ReLU(inplace=True)])
            layers.append(nn.Conv2d(96,12,3,padding=1,bias=False))
            self.itermediate_dncnn=nn.Sequential(*layers)
    class FFDNet(nn.Module):
        def __init__(self):
            super().__init__();self.intermediate_dncnn=Middle()
        def forward(self,image,sigma):
            features=torch.nn.functional.pixel_unshuffle(image,2)
            noise=image.new_full((image.shape[0],3,features.shape[2],features.shape[3]),sigma/255)
            residual=self.intermediate_dncnn.itermediate_dncnn(torch.cat((noise,features),1))
            return torch.nn.functional.pixel_shuffle(residual,2)
    return FFDNet()


class Denoiser:
    def __init__(self,gpu=True):
        import torch
        path=weight_path()
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=WEIGHT_HASH:raise ValueError('上色降噪模型校验失败，请修复阅译安装')
        self.device='cuda' if gpu and torch.cuda.is_available() else 'cpu'
        self.model=build_model()
        weights=torch.load(path,map_location='cpu',weights_only=True)
        self.model.load_state_dict({key.removeprefix('module.'):value for key,value in weights.items()})
        self.model=self.model.to(self.device).eval()

    def process(self,image,sigma,cancel=lambda:False):
        import cv2
        import torch
        if cancel():raise InterruptedError('上色降噪已取消')
        size=image.size;array=np.asarray(image.convert('RGB'))
        # MTU limits FFDNet input to 1200 px, then restores its original size.
        if max(array.shape[:2])>1200:
            ratio=max(array.shape[:2])/1200
            array=cv2.resize(array,(max(1,int(size[0]/ratio)),max(1,int(size[1]/ratio))),interpolation=cv2.INTER_AREA)
        h,w=array.shape[:2]
        array=np.pad(array,((0,h%2),(0,w%2),(0,0)),mode='edge')
        tensor=torch.from_numpy(array.transpose(2,0,1).copy()).unsqueeze(0).to(self.device,dtype=torch.float32)/255
        with inference_threads(self.device),torch.inference_mode():
            output=(tensor-self.model(tensor,sigma)).clamp(0,1)[0,:,:h,:w]
            array=np.rint(output.cpu().numpy().transpose(1,2,0)*255).astype(np.uint8)
        if cancel():raise InterruptedError('上色降噪已取消；尚未提交上色请求')
        if (w,h)!=size:array=cv2.resize(array,size,interpolation=cv2.INTER_LINEAR)
        return Image.fromarray(array)


def prepare_input(owner,source):
    sigma=validate_sigma(owner.settings.color_denoise_sigma)
    if sigma==-1:return source
    if owner.cancel():raise InterruptedError('上色降噪已取消')
    if owner.denoiser is None:owner.denoiser=Denoiser(owner.settings.gpu)
    return owner.denoiser.process(source,sigma,owner.cancel)
