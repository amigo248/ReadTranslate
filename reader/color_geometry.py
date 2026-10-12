"""Restore image geometry without text detection, OCR or source-pixel pasting."""
import cv2
import numpy as np
from PIL import Image

GEOMETRY_VERSION='aspect-v1'


def fit_canvas(image,size):
    """Contain every returned pixel; use one scale and white letterboxing."""
    image=image.convert('RGB')
    if image.size==size:return image.copy()
    scale=min(size[0]/image.width,size[1]/image.height)
    shape=(max(1,min(size[0],round(image.width*scale))),max(1,min(size[1],round(image.height*scale))))
    resized=image.resize(shape,Image.Resampling.LANCZOS)
    canvas=Image.new('RGB',size,'white');canvas.paste(resized,((size[0]-shape[0])//2,(size[1]-shape[1])//2))
    return canvas


def estimate_geometry(source,result):
    """Only accept a well-distributed, near-axis-aligned image correspondence.

    Matching is on page artwork, not recognized characters or text regions.
    Local redraws and weak/clustered matches must not determine a page warp.
    """
    def small(image):
        array=np.asarray(image.convert('RGB'));scale=min(1,1200/max(image.size))
        if scale<1:array=cv2.resize(array,(round(image.width*scale),round(image.height*scale)),interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(array,cv2.COLOR_RGB2GRAY),np.array([array.shape[1]/image.width,array.shape[0]/image.height])
    a,sa=small(source);b,sb=small(result)
    if min(a.shape+b.shape)<24:return None
    detector=cv2.ORB_create(nfeatures=5000)
    ka,da=detector.detectAndCompute(a,None);kb,db=detector.detectAndCompute(b,None)
    if da is None or db is None:return None
    pairs=cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da,db,k=2)
    matches=[pair[0] for pair in pairs if len(pair)==2 and pair[0].distance<.72*pair[1].distance]
    if len(matches)<80:return None
    p=np.float32([ka[m.queryIdx].pt for m in matches]);q=np.float32([kb[m.trainIdx].pt for m in matches])
    matrix,mask=cv2.estimateAffine2D(p,q,method=cv2.RANSAC,ransacReprojThreshold=4,maxIters=3000,confidence=.995)
    if matrix is None or mask is None:return None
    keep=mask.ravel().astype(bool);p=p[keep];q=q[keep]
    if len(p)<60 or keep.mean()<.4:return None
    coverage=np.ptp(p,axis=0)/np.array([a.shape[1],a.shape[0]])
    cells={(min(2,int(x/a.shape[1]*3)),min(2,int(y/a.shape[0]*3))) for x,y in p}
    if coverage.min()<.55 or len(cells)<5:return None
    sx,sy=matrix[0,0],matrix[1,1]
    if sx<=0 or sy<=0 or not .3<sx/sy<3:return None
    if abs(matrix[0,1])/sx>.025 or abs(matrix[1,0])/sy>.025:return None
    errors=np.linalg.norm(p@matrix[:,:2].T+matrix[:,2]-q,axis=1)
    if float(np.quantile(errors,.9))>3.5:return None
    # Restore native pixel units; thumbnail rounding need not be isotropic.
    matrix[:,:2]=matrix[:,:2]*sa[None,:]/sb[:,None];matrix[:,2]/=sb
    return {'matrix':matrix.tolist(),'matches':len(matches),'inliers':len(p),
            'coverage':coverage.tolist(),'grid_cells':len(cells),'error_p90':float(np.quantile(errors,.9)),
            'x_scale':float(matrix[0,0]),'y_scale':float(matrix[1,1])}


def padding_crop(image,info):
    """Crop only a square canvas with visibly intact, near-white padding."""
    if not info or abs(image.width/image.height-1)>.02:return None
    x,y,w,h,side=info
    bounds=(round(x*image.width/side),round(y*image.height/side),round((x+w)*image.width/side),round((y+h)*image.height/side))
    left,top,right,bottom=bounds
    if not 0<=left<right<=image.width or not 0<=top<bottom<=image.height:return None
    array=np.asarray(image.convert('RGB'));strips=[]
    # Check the interior of artificial padding; ignore a one-pixel join.
    if left>2:strips.append(array[:,:left-1])
    if right<image.width-2:strips.append(array[:,right+1:])
    if top>2:strips.append(array[:top-1,:])
    if bottom<image.height-2:strips.append(array[bottom+1:,:])
    if not strips:return None
    if any(float((strip.min(axis=2)>=242).mean())<.995 for strip in strips):return None
    return bounds


def normalize_result(image,size,info=None,source=None):
    image=image.convert('RGB')
    details={'version':GEOMETRY_VERSION,'model_size':list(image.size),'source_size':list(size),'method':'contain',
             'padding_requested':bool(info),'padding_confirmed':False,'warning':''}
    if source is not None:
        try:geometry=estimate_geometry(source,image)
        except (cv2.error,ValueError):geometry=None
        if geometry:
            matrix=np.asarray(geometry['matrix']);details['registration']=geometry
            if image.size==size and np.max(np.abs(matrix[:,:2]-np.eye(2)))<.004 and np.max(np.abs(matrix[:,2]))<2:
                details['method']='verified_unchanged';return image.copy(),details
            inverse=cv2.invertAffineTransform(matrix)
            array=cv2.warpAffine(np.asarray(image),inverse,size,flags=cv2.INTER_LANCZOS4,borderMode=cv2.BORDER_CONSTANT,borderValue=(255,255,255))
            details['method']='verified_registration'
            corners=np.array([[0,0],[size[0]-1,0],[0,size[1]-1],[size[0]-1,size[1]-1]])@matrix[:,:2].T+matrix[:,2]
            if (corners[:,0].min()<-image.width*.01 or corners[:,1].min()<-image.height*.01 or
                corners[:,0].max()>image.width*1.01 or corners[:,1].max()>image.height*1.01):
                details['warning']='已按可靠图像对应关系校正比例；模型或旧版本曾裁去的边缘无法恢复，空缺处保留白边，旧版仍保留。'
            return Image.fromarray(array),details
    crop=padding_crop(image,info)
    if crop:
        image=image.crop(crop);details.update(method='verified_padding',padding_confirmed=True,crop=list(crop))
    elif info:
        details['warning']='模型返回的画布或补白布局无法可靠确认，已保留完整画面并等比补白，未强制拉伸或裁剪。'
    if abs(image.width/image.height-size[0]/size[1])>.01:
        details['warning']=details['warning'] or '模型返回比例与来源不同，已等比补白保存；未强制拉伸。'
    return fit_canvas(image,size),details
