"""Local, conservative speech balloon selection and per-region overrides."""
from __future__ import annotations

import math
import hashlib
from collections import OrderedDict
from threading import RLock
import cv2
import numpy as np


_boundary_cache=OrderedDict()
_boundary_lock=RLock()


def cached_boundaries(image,text_boxes):
    """Reuse exact pixel/box analysis for the last normal and enhanced page.

    Flags, translations and fonts never change physical contours. Keep at most
    two analyses in memory; original text boxes still protect excluded text.
    The serialized render pool also serializes updates to their owner caches.
    """
    key=(image.size,image.mode,hashlib.sha256(image.tobytes()).digest(),
         tuple(tuple(box) for box in text_boxes))
    with _boundary_lock:
        if key in _boundary_cache:
            _boundary_cache.move_to_end(key)
            return _boundary_cache[key]
        bounds=PageBoundaries(image,text_boxes)
        _boundary_cache[key]=bounds
        while len(_boundary_cache)>2:_boundary_cache.popitem(last=False)
        return bounds


class PageBoundaries:
    """Separate text recognition boxes from safe ink/placement boundaries."""
    def __init__(self, image, text_boxes=None):
        self.width,self.height=image.size
        self.scale=min(1.0,1400/self.width,math.sqrt(3_000_000/(self.width*self.height)))
        small=image.resize((max(1,round(self.width*self.scale)),max(1,round(self.height*self.scale))))
        gray=np.asarray(small.convert('L'))
        self.gray=gray
        gray=gray.copy()
        preliminary=PageBoundaries(image) if text_boxes else None
        for box in text_boxes or []:
            if preliminary.owner(box) is None:continue
            x,y,w,h=box
            left,top=max(0,int(x*self.scale)),max(0,int(y*self.scale))
            right,bottom=min(gray.shape[1],math.ceil((x+w)*self.scale)),min(gray.shape[0],math.ceil((y+h)*self.scale))
            if right<=left or bottom<=top:continue
            crop=self.gray[top:bottom,left:right]
            light=float(np.median(crop))>=127
            ink=((crop<150) if light else (crop>180)).astype(np.uint8)
            thick=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((5,5),np.uint8))
            thick=cv2.dilate(thick,np.ones((3,3),np.uint8))
            # Remove bold glyphs only in the analysis copy. Thin frame lines survive.
            gray[top:bottom,left:right][thick>0]=255 if light else 0
        self.count,self.labels,self.stats,_=cv2.connectedComponentsWithStats((gray>=220).astype(np.uint8),8)
        black=(self.gray<110).astype(np.uint8)
        horizontal=cv2.morphologyEx(black,cv2.MORPH_OPEN,np.ones((1,max(40,round(gray.shape[1]*.30))),np.uint8))
        vertical=cv2.morphologyEx(black,cv2.MORPH_OPEN,np.ones((max(60,round(gray.shape[0]*.20)),1),np.uint8))
        self.rails=np.zeros_like(black)
        for candidate,axis in ((horizontal,3),(vertical,2)):
            count,labels,stats,_=cv2.connectedComponentsWithStats(candidate,8)
            for index in range(1,count):
                if stats[index,axis]<=max(12,round(min(gray.shape)*.03)):
                    self.rails[labels==index]=1
        self.shapes={}
        self.layers=[(0,self.labels,self.stats,'light')]
        offset=self.count
        # Seal tiny broken contour gaps without relaxing the ink boundary.
        for size in (3,5,11):
            closed=cv2.morphologyEx((gray<200).astype(np.uint8),cv2.MORPH_CLOSE,np.ones((size,size),np.uint8))
            count,labels,stats,_=cv2.connectedComponentsWithStats(1-closed,8)
            self.layers.append((offset,labels,stats,'light'));offset+=count
        # Opening detaches dark balloons from thin drawing lines; white glyphs are holes.
        dark=cv2.morphologyEx((gray<100).astype(np.uint8),cv2.MORPH_OPEN,np.ones((7,7),np.uint8))
        count,labels,stats,_=cv2.connectedComponentsWithStats(dark,8)
        self.layers.append((offset,labels,stats,'dark'));offset+=count
        dark=cv2.morphologyEx((gray<100).astype(np.uint8),cv2.MORPH_CLOSE,np.ones((5,5),np.uint8))
        dark=cv2.morphologyEx(dark,cv2.MORPH_OPEN,np.ones((7,7),np.uint8))
        count,labels,stats,_=cv2.connectedComponentsWithStats(dark,8)
        self.layers.append((offset,labels,stats,'dark'))
        if preliminary is not None:
            # Bold-ink cleanup can join neighbouring balloons to the artwork.
            # Keep the original enclosed components as independent candidates.
            offset+=count
            self.layers.append((offset,preliminary.labels,preliminary.stats,'light'))
        self.owner_cache={}
        self.ink_distance=None

    def layer(self,label):
        for offset,labels,stats,tone in reversed(self.layers):
            if offset<label<offset+len(stats):return offset,labels,stats,tone
        raise ValueError('Invalid boundary label')

    def tone(self,label):
        return self.layer(label)[3] if label else 'unknown'

    def text_tone(self,box):
        """Infer contrast without treating a manual text box as a new boundary."""
        owner=self.owner(box)
        if owner:return self.tone(owner)
        left,top,right,bottom=self.small_box(box)
        crop=self.gray[top:bottom,left:right]
        if crop.size and float((crop<=80).mean())>=.60:return 'dark'
        if crop.size and float((crop>=220).mean())>=.60:return 'light'
        return 'unknown'

    def small_box(self,box):
        x,y,w,h=box
        ih,iw=self.labels.shape
        return max(0,int(x*self.scale)),max(0,int(y*self.scale)),min(iw,math.ceil((x+w)*self.scale)),min(ih,math.ceil((y+h)*self.scale))

    def shape(self,label):
        if label in self.shapes:return self.shapes[label]
        result=None
        if label:
            offset,labels,stats,tone=self.layer(label)
            index=label-offset
            x,y,w,h,area=map(int,stats[index])
            ih,iw=labels.shape
            # Enclosure and uniform tone define a frame, rather than oval shape,
            # aspect ratio or how much of the OCR rectangle it happens to fill.
            touches=sum((x==0,y==0,x+w>=iw,y+h>=ih))
            if w>=12 and h>=12 and 80<=area<iw*ih*.90 and touches<3:
                component=(labels[y:y+h,x:x+w]==index).astype(np.uint8)
                contours,hierarchy=cv2.findContours(component,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
                if contours:
                    contour=max(contours,key=cv2.contourArea)
                    fill=np.zeros_like(component)
                    cv2.drawContours(fill,[contour],-1,1,cv2.FILLED)
                    if tone=='light' and hierarchy is not None:
                        for index,child in enumerate(contours):
                            if hierarchy[0,index,3]<0 or cv2.contourArea(child)<fill.sum()*.03:continue
                            hole=np.zeros_like(fill);cv2.drawContours(hole,[child],-1,1,cv2.FILLED)
                            # Panel backgrounds can surround another whole text frame.
                            # They must not inherit that inner frame as their own.
                            if float((self.gray[y:y+h,x:x+w][hole>0]>220).mean())>.70:
                                self.shapes[label]=None
                                return None
                    if area/max(1,int(fill.sum()))<.50:
                        self.shapes[label]=None
                        return None
                    padded=np.pad(fill,1)
                    ring=(cv2.dilate(padded,np.ones((3,3),np.uint8))-padded)>0
                    context=np.zeros_like(padded,dtype=np.uint8)
                    left,top,right,bottom=max(0,x-1),max(0,y-1),min(iw,x+w+1),min(ih,y+h+1)
                    context[top-y+1:bottom-y+1,left-x+1:right-x+1]=self.gray[top:bottom,left:right]
                    if tone=='light' and (not ring.any() or float((context[ring]<190).mean())<.70):
                        self.shapes[label]=None
                        return None
                    if tone=='dark':
                        white_ink=(self.gray[y:y+h,x:x+w]>200)&(fill>0)
                        # A solid black symbol has no white lettering inside it.
                        if white_ink.sum()<15:
                            self.shapes[label]=None
                            return None
                        # A clear contrasting rim works on both white and grey surroundings.
                        if not ring.any() or float((context[ring]>130).mean())<.60:
                            self.shapes[label]=None
                            return None
                    # Fill character holes, then leave space from the actual contour.
                    enclosed=fill.copy()
                    fill=cv2.erode(fill,np.ones((3,3),np.uint8),iterations=max(1,round(3*self.scale)),borderType=cv2.BORDER_CONSTANT,borderValue=0)
                    if tone=='light':
                        if self.ink_distance is None:
                            self.ink_distance=cv2.distanceTransform((self.gray<180).astype(np.uint8),cv2.DIST_L2,5)
                        near_edge=cv2.distanceTransform(fill,cv2.DIST_L2,5)<=6
                        thin=(self.ink_distance[y:y+h,x:x+w]<=2)&(self.gray[y:y+h,x:x+w]<180)
                        original_ink=(self.gray[y:y+h,x:x+w]<180).astype(np.uint8)
                        _,ink_labels=cv2.connectedComponents(original_ink,8)
                        outside=enclosed==0
                        outside[[0,-1],:]=True;outside[:,[0,-1]]=True
                        rim_ids=np.unique(ink_labels[outside&(original_ink>0)])
                        # A letter near the rim is still text unless its ink
                        # actually connects to the outer frame/drawing.
                        frame_ink=np.isin(ink_labels,rim_ids)
                        fill[near_edge&thin&frame_ink]=0
                    values=self.gray[y:y+h,x:x+w][fill>0]
                    if not len(values) or float(((values>80)&(values<220)).mean())>.18:
                        self.shapes[label]=None
                        return None
                    if tone=='light' and (values<110).sum()<15:
                        self.shapes[label]=None
                        return None
                    result=(x,y,fill)
        self.shapes[label]=result
        return result

    def shape_crop(self,label,box):
        x0,y0,x1,y1=self.small_box(box)
        crop=np.zeros((max(0,y1-y0),max(0,x1-x0)),np.uint8)
        shape=self.shape(label)
        if shape is not None:
            x,y,mask=shape
            left,top,right,bottom=max(x0,x),max(y0,y),min(x1,x+mask.shape[1]),min(y1,y+mask.shape[0])
            if right>left and bottom>top:crop[top-y0:bottom-y0,left-x0:right-x0]=mask[top-y:bottom-y,left-x:right-x]
        w,h=map(int,box[2:])
        return cv2.resize(crop,(w,h),interpolation=cv2.INTER_NEAREST) if crop.size and w>0 and h>0 else np.zeros((max(0,h),max(0,w)),np.uint8)

    def labels_in(self,box):
        x0,y0,x1,y1=self.small_box(box)
        result=[]
        for offset,labels,stats,tone in self.layers[1:]+self.layers[:1]:
            sample=labels[y0:y1,x0:x1]
            if not sample.size:continue
            ids,counts=np.unique(sample,return_counts=True)
            result.extend(int(i)+offset for i,c in sorted(zip(ids,counts),key=lambda v:-v[1])
                          if i and c>=sample.size*.025 and self.shape(int(i)+offset) is not None)
        return sorted(result,key=lambda label:int(self.shape(label)[2].sum()))

    def owner(self,box):
        key=tuple(box)
        if key in self.owner_cache:return self.owner_cache[key]
        best=None;best_area=0
        for label in self.labels_in(box):
            mask=self.shape_crop(label,box)
            shape=self.shape(label)
            coverage=float(mask.mean())
            contained=float(mask.sum())/max(1,int(shape[2].sum())/self.scale**2)
            if ((coverage>=.30 and mask[mask.shape[0]//2,mask.shape[1]//2])
                or (coverage>=.15 and contained>=.65)
                or (self.tone(label)=='dark' and coverage>=.15)):
                _,_,rw,rh=largest_inner_rectangle(mask)
                if rw*rh>best_area:best=label;best_area=rw*rh
        self.owner_cache[key]=best
        return best

    def recognition_box(self,box,label):
        """Trim a detector rectangle to the actual enclosed background."""
        mask=self.recognition_mask(box,label)
        ys,xs=np.where(mask>0)
        if not len(xs):return box
        x,y,w,h=box
        return [int(x+xs.min()),int(y+ys.min()),int(xs.max()-xs.min()+1),int(ys.max()-ys.min()+1)]

    def recognition_mask(self,box,label):
        # OCR needs the full original lettering, including ink close to the rim.
        # Rendering still uses the stricter, eroded shape.
        return cv2.dilate(self.shape_crop(label,box),np.ones((7,7),np.uint8))

    def panel(self,box):
        x,y,w,h=box
        sx=max(0,min(self.labels.shape[1]-1,round((x+w/2)*self.scale)))
        sy=max(0,min(self.labels.shape[0]-1,round((y+h/2)*self.scale)))
        rows=np.flatnonzero(self.rails[:,sx]);columns=np.flatnonzero(self.rails[sy,:])
        left=int(columns[columns<sx].max()+1) if np.any(columns<sx) else 0
        right=int(columns[columns>sx].min()) if np.any(columns>sx) else self.labels.shape[1]
        top=int(rows[rows<sy].max()+1) if np.any(rows<sy) else 0
        bottom=int(rows[rows>sy].min()) if np.any(rows>sy) else self.labels.shape[0]
        return (math.ceil(left/self.scale),math.ceil(top/self.scale),min(self.width,int(right/self.scale)),min(self.height,int(bottom/self.scale)))

    def crosses_frame(self,box):
        owner=self.owner(box)
        if owner and self.tone(owner)=='dark':return False
        x0,y0,x1,y1=self.small_box(box)
        if x1-x0<6 or y1-y0<6:return False
        middle=self.rails[y0+2:y1-2,x0+2:x1-2]
        return bool(middle.size and (np.max(middle.sum(axis=0))>middle.shape[0]*.6 or np.max(middle.sum(axis=1))>middle.shape[1]*.6))

    def allowed(self,box):
        x,y,w,h=map(int,box)
        mask=np.ones((h,w),np.uint8)
        owner=self.owner(box)
        if owner:
            mask=self.shape_crop(owner,box)
        else:
            left,top,right,bottom=self.panel(box)
            # Page margins and panel rules are hard constraints, even for sound effects.
            left,top,right,bottom=max(2,left+2),max(2,top+2),min(self.width-2,right-2),min(self.height-2,bottom-2)
            yy,xx=np.ogrid[y:y+h,x:x+w]
            mask=((xx>=left)&(xx<right)&(yy>=top)&(yy<bottom)).astype(np.uint8)
            for label in self.labels_in(box):mask[self.shape_crop(label,box)>0]=0
        x0,y0,x1,y1=self.small_box(box)
        rails=self.rails[y0:y1,x0:x1]
        if rails.size and not (owner and self.tone(owner)=='dark'):
            rails=cv2.resize(rails,(w,h),interpolation=cv2.INTER_NEAREST)
            mask[rails>0]=0
        return mask,owner


def largest_inner_rectangle(mask):
    """An axis-aligned rectangle whose every pixel is inside the supplied boundary."""
    # Full-page safe masks contain one small balloon. Skip empty page margins
    # instead of visiting millions of zero pixels in the Python histogram loop.
    rows=np.flatnonzero(np.any(mask>0,axis=1));columns=np.flatnonzero(np.any(mask>0,axis=0))
    if not len(rows) or not len(columns):return [0,0,0,0]
    ox,oy=int(columns[0]),int(rows[0])
    mask=mask[oy:int(rows[-1])+1,ox:int(columns[-1])+1]
    heights=np.zeros(mask.shape[1],dtype=int)
    best=(0,0,0,0);area=0
    for y,row in enumerate(mask):
        heights=np.where(row>0,heights+1,0)
        stack=[]
        for x in range(len(heights)+1):
            value=int(heights[x]) if x<len(heights) else 0
            start=x
            while stack and stack[-1][1]>value:
                index,height=stack.pop();start=index
                candidate=height*(x-index)
                if candidate>area:area=candidate;best=(index,y-height+1,x-index,height)
            if value and (not stack or stack[-1][1]<value):stack.append((start,value))
    return [best[0]+ox,best[1]+oy,best[2],best[3]]


def white_frame_boxes(image, regions, *, missing_frames_only=False):
    """Find ink in enclosed white backgrounds even when the detector misses it.

    Work on the original page so existing OCR never defines which frames exist.
    On saved pages, only add previously absent frames, preserving reviewed text.
    """
    bounds=PageBoundaries(image)
    covered=np.zeros(bounds.gray.shape,np.uint8)
    for region in regions:
        x0,y0,x1,y1=bounds.small_box(region['box'])
        covered[y0:y1,x0:x1]=1
    candidates=[]
    for offset,labels,stats,tone in bounds.layers:
        if tone!='light':continue
        for index in range(1,len(stats)):
            x,y,w,h,area=map(int,stats[index])
            if w<12 or h<12 or area<80:continue
            shape=bounds.shape(offset+index)
            if shape is None or w/bounds.scale<24 or h/bounds.scale<36 or shape[2].sum()<300*bounds.scale**2:continue
            # A glyph counter / highlight is not a framed text background.
            component=(labels[y:y+h,x:x+w]==index).astype(np.uint8)
            contours,_=cv2.findContours(component,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            fill=np.zeros_like(component);cv2.drawContours(fill,[max(contours,key=cv2.contourArea)],-1,1,cv2.FILLED)
            padded=np.pad(fill,1);ring=(cv2.dilate(padded,np.ones((3,3),np.uint8))-padded)>0
            context=np.pad(bounds.gray,1)[y:y+h+2,x:x+w+2]
            if float((context[ring]<190).mean())<.88:continue
            candidates.append((int(shape[2].sum()),offset+index,shape))
    boxes=[]
    for _,label,(x,y,mask) in sorted(candidates):
        ink=((bounds.gray[y:y+mask.shape[0],x:x+mask.shape[1]]<150)&(mask>0)).astype(np.uint8)
        count,components,stats,_=cv2.connectedComponentsWithStats(ink,8)
        clean=np.zeros_like(ink)
        for index in range(1,count):
            a,b,w,h,area=map(int,stats[index])
            if area>=max(4,round(5*bounds.scale**2)) and h>=3 and w>=2:
                clean[components==index]=1
        total=int(clean.sum())
        if total<20 or total/max(1,int(mask.sum()))<.012:continue
        known=covered[y:y+mask.shape[0],x:x+mask.shape[1]]
        if missing_frames_only and np.any(clean&known):continue
        missing=clean&(1-known)
        if int(missing.sum())<20 or float(missing.sum())/total<.10:continue
        # Recognize the whole compartment, including short marks/second columns,
        # and give Chinese fitting the actual available space of the white frame.
        left=max(0,math.floor(x/bounds.scale));top=max(0,math.floor(y/bounds.scale))
        right=min(image.width,math.ceil((x+mask.shape[1])/bounds.scale))
        bottom=min(image.height,math.ceil((y+mask.shape[0])/bounds.scale))
        box=[left,top,right-left,bottom-top]
        boxes.append(box)
        # Suppress duplicates from other contour-repair layers.
        known[clean>0]=1
    return boxes


def prioritize_white_regions(image, regions):
    bounds=PageBoundaries(image,[r['box'] for r in regions])
    return sorted(regions,key=lambda r: {'light':0,'dark':1,'unknown':2}[bounds.tone(bounds.owner(r['box']))])


def needs_regrouping(image,regions):
    bounds=PageBoundaries(image,[r['box'] for r in regions])
    for r in regions:
        if r.get('selection','auto')=='include' or r.get('mode','inpaint')!='inpaint':continue
        if not r.get('text_polygons'):
            if bounds.crosses_frame(r['box']) or (bounds.owner(r['box']) is None and bounds.labels_in(r['box'])):return True
        else:
            owners=set()
            for polygon in r['text_polygons']:
                p=np.asarray(polygon);a,b=p.min(axis=0),p.max(axis=0)
                owners.add(bounds.owner([int(a[0]),int(a[1]),max(1,int(b[0]-a[0])),max(1,int(b[1]-a[1]))]))
            if len(owners)>1 and any(o is not None for o in owners):return True
    return False


def annotate_boundaries(image,regions,boundaries=None,offset=(0,0)):
    bounds=boundaries or PageBoundaries(image,[r['box'] for r in regions])
    for region in regions:
        region.pop('boundary_warning',None)
        if region.get('selection')=='exclude' or region.get('mode')=='keep':continue
        x,y,w,h=region['box'];box=[x+offset[0],y+offset[1],w,h]
        # Automatic OCR may identify drawing/sound effects too. Without a confirmed
        # compartment, retain the original rather than invent a dialogue position.
        if (region.get('text') and region.get('mode','inpaint')=='inpaint' and region.get('selection','auto')!='include'
            and bounds.owner(box) is None):
            region['boundary_warning']='未确认气泡/文字框边界，已保留原文；请框选或校对后明确翻译此框。'


def original_orientation(region: dict) -> str:
    explicit = region.get("source_orientation")
    if explicit in {"vertical", "horizontal"}:
        return explicit
    _, _, width, height = region["box"]
    return "vertical" if height > width * 1.15 else "horizontal"


def text_orientation(region: dict, default="auto") -> str:
    value = region.get("layout", "auto")
    if value not in {"vertical", "horizontal"}:
        value = default
    return value if value in {"vertical", "horizontal"} else original_orientation(region)


def region_enabled(region: dict, scope="dialogue") -> bool:
    if region.get("mode") == "keep" or region.get("selection") == "exclude":
        return False
    if region.get("selection") == "include":
        return True
    if region.get('boundary_warning'):
        return False
    return scope == "all" or region.get("is_dialogue") is True


def annotate_regions(image, regions: list[dict]) -> None:
    """Every enclosed white/black text background is eligible, including captions."""
    pending = [r for r in regions if r.get("balloon_analysis") != 4 or r.get("analysis_box") != r["box"]]
    if not pending:return
    bounds=PageBoundaries(image,[r['box'] for r in regions])
    for region in pending:
        region["is_dialogue"] = bounds.owner(region["box"]) is not None
        region["balloon_analysis"] = 4
        region["analysis_box"] = list(region["box"])
        region.setdefault("source_orientation", original_orientation(region))
        region.setdefault("selection", "auto")
        region.setdefault("layout", "auto")
