"""User-selected placement areas, independent of source OCR coordinates."""
import math
import cv2
import numpy as np


def clipped_box(box,size):
    if len(box)!=4 or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in box):
        raise ValueError('请框选有效的译文范围')
    x,y,w,h=map(round,box)
    if w<6 or h<6:raise ValueError('请框选至少 6×6 像素的译文范围')
    left,top=max(0,x),max(0,y);right,bottom=min(size[0],x+w),min(size[1],y+h)
    if right-left<6 or bottom-top<6:raise ValueError('译文范围不在图片内')
    return [left,top,right-left,bottom-top]


def enlarged_box(source,chosen,size):
    chosen=clipped_box(chosen,size)
    x,y,w,h=source;a,b,c,d=chosen
    left,top=min(x,a),min(y,b);right,bottom=max(x+w,a+c),max(y+h,b+d)
    return clipped_box([left,top,right-left,bottom-top],size)


def layout_mask(bounds,source,area):
    """Anchor an enlarged area to its source balloon/panel, never its neighbor."""
    owner=bounds.owner(source)
    x,y,w,h=map(int,area)
    if owner:allowed=bounds.shape_crop(owner,area)
    else:
        left,top,right,bottom=bounds.panel(source)
        left,top=max(2,left+2),max(2,top+2)
        right,bottom=min(bounds.width-2,right-2),min(bounds.height-2,bottom-2)
        yy,xx=np.ogrid[y:y+h,x:x+w]
        allowed=((xx>=left)&(xx<right)&(yy>=top)&(yy<bottom)).astype(np.uint8)
        for label in bounds.labels_in(area):allowed[bounds.shape_crop(label,area)>0]=0
    x0,y0,x1,y1=bounds.small_box(area);rails=bounds.rails[y0:y1,x0:x1]
    if rails.size and not (owner and bounds.tone(owner)=='dark'):
        allowed[cv2.resize(rails,(w,h),interpolation=cv2.INTER_NEAREST)>0]=0
    return allowed,owner


def grow_selection(selection,global_box,size):
    """Rebase every patch-local coordinate while preserving its original pixels."""
    old=selection['box'];new=enlarged_box(old,global_box,size)
    selection.setdefault('recognition_box',list(old))
    dx,dy=old[0]-new[0],old[1]-new[1]
    for region in selection.get('regions',[]):
        for key in ('box','layout_box','render_box'):
            if key in region:region[key]=[region[key][0]+dx,region[key][1]+dy,*region[key][2:]]
        if isinstance(region.get('render_overflow'),dict) and 'box' in region['render_overflow']:
            issue=region['render_overflow'];issue['box']=[issue['box'][0]+dx,issue['box'][1]+dy,*issue['box'][2:]]
        if region.get('text_polygons'):
            region['text_polygons']=[[[x+dx,y+dy] for x,y in polygon] for polygon in region['text_polygons']]
    selection['box']=new
    return new


def covered_by_selection(region,selection):
    x,y,w,h=region['box'];a,b,c,d=selection.get('recognition_box',selection['box'])
    return x>=a and y>=b and x+w<=a+c and y+h<=b+d


def composite_selection(original,current,patch,selection,previous_regions=()):
    """Expanded crops must preserve unrelated translations in their padding."""
    from PIL import Image
    from .comic_regions import PageBoundaries
    x,y,w,h=selection['box']
    base=np.asarray(current).copy();source=np.asarray(original)
    bounds=PageBoundaries(original)
    for region,offset in previous_regions:
        if not region.get('render_box'):continue
        a,b,c,d=region['render_box'];a+=offset[0];b+=offset[1]
        left,top=max(x,a),max(y,b);right,bottom=min(x+w,a+c),min(y+h,b+d)
        if right<=left or bottom<=top:continue
        sx,sy,sw,sh=region['box'];sx+=offset[0];sy+=offset[1]
        allowed,_=layout_mask(bounds,[sx,sy,sw,sh],[left,top,right-left,bottom-top])
        view=base[top:bottom,left:right];view[allowed>0]=source[top:bottom,left:right][allowed>0]
    pixels=np.asarray(patch);changed=np.any(pixels!=source[y:y+h,x:x+w],axis=2)
    base[y:y+h,x:x+w][changed]=pixels[changed]
    return Image.fromarray(base)
