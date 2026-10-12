from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .comic_regions import annotate_regions, region_enabled, text_orientation, PageBoundaries, largest_inner_rectangle, annotate_boundaries
from .layout_status import TextOverflowError,mark_overflow,clear_overflow
from .translation import clean_translation


def load_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


OPEN_PUNCTUATION = "（(「『“《【〈﹁﹃︵︽︻"
CLOSE_PUNCTUATION = "，、。！？：；,.!?;:）)」』”》】〉…︐︑︒︓︔︕︖︙︶﹂﹄︾︼"


def wrapped_text(text: str, font, width: int) -> list[str]:
    lines = []
    fallbacks = {}
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
        while paragraph:
            end=1
            while end<len(paragraph) and glyph_text_width(paragraph[:end+1],font,fallbacks)<=width:
                end+=1
            if end<len(paragraph) and end>1:
                if paragraph[end] in CLOSE_PUNCTUATION or paragraph[end-1] in OPEN_PUNCTUATION:
                    end-=1
            lines.append(paragraph[:end])
            paragraph=paragraph[end:]
    return lines


def layout_text(text: str, width: int, height: int, font_path: str, requested_size=0, *, maximum_size=64, spacing=1.22):
    from .embedded_runtime import resolve_font
    font_path=resolve_font(font_path)
    if not Path(font_path).is_file():
        raise ValueError("找不到中文字体，请在设置中选择 .ttf/.ttc 字体")
    start = min(maximum_size, max(12, int(height / 2))) if not requested_size else requested_size
    sizes = [start] if requested_size else range(start, 7, -1)
    for size in sizes:
        font = ImageFont.truetype(font_path, size)
        lines = wrapped_text(text, font, max(1, width))
        advance = math.ceil(size * spacing)
        if len(lines) * advance <= height and all(glyph_text_width(line,font) <= width for line in lines):
            return font, lines, advance
    raise ValueError("译文超出文字框，请扩大文字框、缩小字号或简化译文后重新排版")


VERTICAL_FORMS = str.maketrans({
    "，": "︐", ",": "︐", "、": "︑", "。": "︒", "：": "︓", ":": "︓",
    "；": "︔", ";": "︔", "！": "︕", "!": "︕", "？": "︖", "?": "︖",
    "…": "︙", "—": "︱", "–": "︲", "（": "︵", "）": "︶",
    "(": "︵", ")": "︶", "「": "﹁", "」": "﹂", "『": "﹃", "』": "﹄",
    "“": "﹁", "”": "﹂", "《": "︽", "》": "︾", "【": "︻", "】": "︼",
})


def has_glyph(font, character):
    mask=font.getmask(character)
    missing=font.getmask("\U0010ffff")
    return mask.size!=missing.size or bytes(mask)!=bytes(missing)


class MissingGlyphError(ValueError):
    pass


def vertical_glyph_font(font, character, fallbacks):
    key = ('glyph', character)
    if key in fallbacks:return fallbacks[key]
    if has_glyph(font,character):
        fallbacks[key] = font
        return font
    from .embedded_runtime import resolve_font
    for path in (resolve_font('builtin'),resolve_font('builtin-yahei'),resolve_font('builtin-unicode'),
                 "C:/Windows/Fonts/msyh.ttc","C:/Windows/Fonts/simsun.ttc",
                 "C:/Windows/Fonts/seguisym.ttf","C:/Windows/Fonts/arial.ttf"):
        if not Path(path).is_file():continue
        if path not in fallbacks:fallbacks[path]=ImageFont.truetype(path,font.size)
        if has_glyph(fallbacks[path],character):
            fallbacks[key] = fallbacks[path]
            return fallbacks[path]
    raise MissingGlyphError(f"字体缺少字符 U+{ord(character):04X}，请在设置中选择支持该字符的字体；原图已保留。")


def glyph_runs(text, font, fallbacks):
    runs = []
    for character in text:
        selected = vertical_glyph_font(font, character, fallbacks)
        if runs and runs[-1][0] is selected:
            runs[-1] = (selected, runs[-1][1] + character)
        else:runs.append((selected, character))
    return runs


def glyph_text_width(text, font, fallbacks=None):
    return sum(selected.getlength(run) for selected,run in glyph_runs(text,font,{} if fallbacks is None else fallbacks))


def draw_horizontal(draw, text, x, top, font, color, stroke):
    runs = glyph_runs(text, font, {})
    if not runs:return
    baseline = top - min(selected.getbbox(run,anchor='ls')[1] for selected,run in runs)
    for selected,run in runs:
        draw.text((x,baseline),run,font=selected,fill=color,anchor='ls',stroke_width=stroke,stroke_fill=color)
        x += selected.getlength(run)


def vertical_layout(text: str, width: int, height: int, font_path: str, requested_size=0, *, maximum_size=64, spacing=1.15):
    from .embedded_runtime import resolve_font
    font_path=resolve_font(font_path)
    if not Path(font_path).is_file():
        raise ValueError("找不到中文字体，请在设置中选择 .ttf/.ttc 字体")
    start = min(maximum_size, max(12, width)) if not requested_size else requested_size
    for size in ([start] if requested_size else range(start, 7, -1)):
        font = ImageFont.truetype(font_path, size)
        advance = math.ceil(size * spacing)
        capacity = height // advance
        if capacity < 1:
            continue
        columns = []
        for paragraph in text.replace("\r", "").translate(VERTICAL_FORMS).split("\n"):
            if not paragraph:
                columns.append("")
                continue
            # Balance columns so a final one-character column is not stranded.
            length = math.ceil(len(paragraph) / math.ceil(len(paragraph) / capacity))
            while paragraph:
                end = min(length, len(paragraph))
                if end < len(paragraph):
                    if paragraph[end] in "︐︑︒︓︔︕︖︙︶﹂﹄︾︼":
                        end = end + 1 if end < capacity else max(1, end - 1)
                    if end > 1 and paragraph[end-1] in "︵﹁﹃︽︻":
                        end -= 1
                columns.append(paragraph[:end])
                paragraph = paragraph[end:]
        if len(columns) * advance <= width and all(len(column) <= capacity for column in columns):
            return font, columns, advance
    raise ValueError("译文超出文字框，请扩大文字框、缩小字号或简化译文后重新排版")


def draw_vertical(draw, text, box, font_path, requested_size=0, color="#111111", *, maximum_size=64, spacing=1.15, stroke=0, center_columns=False):
    x, y, width, height = box
    pad = max(2, min(8, int(min(width, height) / 18))) + stroke
    font, columns, advance = vertical_layout(text, int(width)-pad*2, int(height)-pad*2, font_path, requested_size,
                                           maximum_size=maximum_size, spacing=spacing)
    left = x + (width - len(columns) * advance) / 2
    fallbacks={}
    # The first column is on the right, every character stays upright.
    for index, column in enumerate(columns):
        cx = left + (len(columns)-1-index) * advance
        top = y + (height - (len(column) if center_columns else max(map(len, columns))) * advance) / 2
        actual_stroke=min(stroke,max(1,round(font.size*.025))) if stroke else 0
        for row, character in enumerate(column):
            glyph_font=vertical_glyph_font(font,character,fallbacks)
            bounds = glyph_font.getbbox(character)
            gx = cx + (advance - (bounds[2]-bounds[0])) / 2 - bounds[0]
            gy = top + row*advance + (advance - (bounds[3]-bounds[1])) / 2 - bounds[1]
            draw.text((gx, gy), character, font=glyph_font, fill=color, stroke_width=actual_stroke, stroke_fill=color)


def source_typography(image, region):
    """Estimate ink height and weight locally; never mistake these for an exact font ID."""
    x,y,w,h = map(int, region['box'])
    gray = np.asarray(image.crop((x,y,x+w,y+h)).convert('L'))
    if not gray.size:
        return 20, False
    ink = (gray < 140).astype(np.uint8)
    if gray.mean() < 100:
        ink = (gray > 180).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    glyphs, strokes = [], []
    for index, (a,b,c,d,area) in enumerate(stats[1:], 1):
        # Ignore clipped borders, small punctuation, and tall balloon contours.
        if a<1 or b<1 or a+c>=w or b+d>=h or c<5 or d<7 or area<15:
            continue
        if not .3 < c/d < 2 or max(c,d)>min(110,max(w,h)*.45):
            continue
        glyphs.append(max(c,d))
        mask=(labels[b:b+d,a:a+c]==index).astype(np.uint8)
        distance=cv2.distanceTransform(np.pad(mask,1),cv2.DIST_L2,5)
        peaks=distance[(distance>=cv2.dilate(distance,np.ones((3,3),np.uint8))) & (distance>0)]
        if len(peaks):strokes.append(float(np.median(peaks))*2/max(c,d))
    detected=region.get('source_font_size',0)
    estimated=float(np.percentile(glyphs,65)) if len(glyphs)>=3 else 0
    # Detector column widths include a small margin; glyph ink gives a second estimate.
    base=estimated or (float(detected)*.9 if detected else 0)
    if not base:
        length=max(1,len(region.get('text','')))
        base=min(min(w,h), math.sqrt(max(1,w*h)/length))
    return max(8,min(128*region.get('render_scale',1),round(base*1.18))), bool(strokes and np.median(strokes)>.17)


def fitted_ink(region, font_path, layout, style, original, rectangle):
    from .embedded_runtime import resolve_font
    font_path=resolve_font(region.get('font_path') or font_path)
    """Shrink even a requested size until the full glyphs fit, rather than clipping ink."""
    x,y,w,h=rectangle
    reference=region.get('style',style)=='reference'
    maximum,detected_bold=source_typography(original,region) if reference else (64,False)
    start=int(region.get('font_size',0)) or maximum
    bold=region.get('weight','auto')=='bold' or (region.get('weight','auto')=='auto' and reference and detected_bold)
    if not Path(font_path).is_file():raise ValueError('找不到中文字体，请在设置中选择 .ttf/.ttc 字体')
    guard=8
    for size in range(max(8,start),7,-1):
        ink=Image.new('RGBA',(w+guard*2,h+guard*2))
        draw=ImageDraw.Draw(ink)
        stroke=max(1,round(size*.025)) if bold and (size>=24 or region.get('weight')=='bold') else 0
        color=region.get('render_color',region.get('color','#111111'))
        try:
            if text_orientation(region,layout)=='vertical':
                draw_vertical(draw,region['translation'],[guard,guard,w,h],font_path,size,color,
                              spacing=1.12 if reference else 1.15,stroke=stroke,center_columns=reference)
            else:
                pad=max(2,min(8,int(w/18)))+stroke
                font,lines,advance=layout_text(region['translation'],w-pad*2,h-pad*2,font_path,size)
                top=guard+(h-len(lines)*advance)/2
                for line in lines:
                    draw_horizontal(draw,line,guard+(w-glyph_text_width(line,font))/2,top,font,color,stroke)
                    top+=advance
        except MissingGlyphError:
            raise
        except ValueError:
            continue
        alpha=np.asarray(ink.getchannel('A')).copy()
        alpha[guard:guard+h,guard:guard+w]=0
        if not alpha.any():
            region['render_font_size']=size
            region['render_box']=list(rectangle)
            return ink.crop((guard,guard,guard+w,guard+h))
    raise TextOverflowError('译文已缩小至 8 px，仍无法完整放入安全边界；中文已保存在校对界面，请缩短译文或调整排版后重新生成。')


def render_image(image, regions: list[dict], font_path: str, scope="all", layout="auto", style="reference", *, boundaries=None, boundary_offset=(0,0), cleanup=None, clean_only=False, backend=None,gpu=True):
    for region in regions:
        if region.get('translation'):
            meta=region.get('global_replacement')
            if not meta or region['translation']!=meta.get('result'):
                region['translation'] = clean_translation(region['translation'], region.get('text', ''))
    if scope == "dialogue":
        annotate_regions(image, regions)
    original = image
    pixels = np.asarray(image).copy()
    height, width = pixels.shape[:2]
    boundaries=boundaries or PageBoundaries(image,[r['box'] for r in regions])
    for index,region in enumerate(regions):
        if (region.get('translation') and not region.get('text_polygons') and region.get('selection','auto')!='include'
            and region.get('mode','inpaint')=='inpaint'):
            x,y,w,h=region['box'];box=[x+boundary_offset[0],y+boundary_offset[1],w,h]
            if boundaries.crosses_frame(box) or (boundaries.owner(box) is None and boundaries.labels_in(box)):
                raise ValueError(f'第 {index+1} 个文字框混入气泡外文字或跨越边框，请重新识别当前页，或使用框选翻译。')
    annotate_boundaries(image,regions,boundaries,boundary_offset)
    plans=[]
    from .text_replace import has_translated_text
    for index,region in enumerate(regions):
        if not has_translated_text(region) or not region_enabled(region, scope):
            if not clean_only:clear_overflow(region)
            continue
        x, y, box_width, box_height = [int(value) for value in region["box"]]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + box_width), min(height, y + box_height)
        if x1 <= x0 or y1 <= y0:
            raise ValueError("文字框超出图片，请调整坐标")
        ox,oy=boundary_offset
        allowed,owner=boundaries.allowed([x0+ox,y0+oy,x1-x0,y1-y0])
        region['render_color'] = ('#f5f5f5' if boundaries.text_tone([x0+ox,y0+oy,x1-x0,y1-y0])=='dark' and region.get('color','#111111')=='#111111'
                                  else region.get('color','#111111'))
        if not owner and not region.get('text_polygons') and region.get('selection','auto')!='include' and region.get('mode','inpaint')=='inpaint' and (boundaries.crosses_frame([x0+ox,y0+oy,x1-x0,y1-y0]) or boundaries.labels_in([x0+ox,y0+oy,x1-x0,y1-y0])):
            raise ValueError(f'第 {index+1} 个文字框混入气泡外文字或跨越边框，请重新识别当前页，或使用框选翻译。')
        if not owner and region.get('text_polygons'):
            speech=np.zeros_like(allowed)
            for label in boundaries.labels_in([x0+ox,y0+oy,x1-x0,y1-y0]):
                speech|=boundaries.shape_crop(label,[x0+ox,y0+oy,x1-x0,y1-y0])
            if speech.mean()>.20:
                raise ValueError(f'第 {index+1} 个文字框的气泡边界不明确，请缩小框选范围后校对，避免把译文放到气泡外。')
        lx,ly=x0,y0;placement=allowed
        if region.get('layout_box') and not clean_only:
            from .layout_area import clipped_box,layout_mask
            lx,ly,lw,lh=clipped_box(region['layout_box'],original.size)
            placement,_=layout_mask(boundaries,[x0+ox,y0+oy,x1-x0,y1-y0],[lx+ox,ly+oy,lw,lh])
            for other in regions:
                if other is region:continue
                a,b,c,d=other.get('layout_box') or other['box']
                left,top=max(lx,a),max(ly,b);right,bottom=min(lx+lw,a+c),min(ly+lh,b+d)
                if right>left and bottom>top:placement[top-ly:bottom-ly,left-lx:right-lx]=0
        bx,by,bw,bh=largest_inner_rectangle(placement)
        if bw<6 or bh<6:
            mark_overflow(region,'文字框没有足够的安全排版空间，请调整文字框或排版后重新生成。')
            continue
        if clean_only:ink=None
        elif region.get('translation','').strip():
            try:ink=fitted_ink(region,font_path,layout,style,original,[lx+bx,ly+by,bw,bh])
            except TextOverflowError as error:
                mark_overflow(region,error,[lx+bx,ly+by,bw,bh])
                continue # Preserve the original ink; still process every other frame.
            clear_overflow(region)
        else:
            ink=Image.new('RGBA',(bw,bh));region['render_font_size']=0;region['render_box']=[lx+bx,ly+by,bw,bh]
            clear_overflow(region)
        if ink is not None:plans.append((ink,lx+bx,ly+by))
        if region.get("mode", "inpaint") == "white":
            pixels[y0:y1, x0:x1][allowed>0] = 255
        elif region.get("mode")=="solid":
            pixels[y0:y1, x0:x1][allowed>0] = np.median(pixels[y0:y1,x0:x1].reshape(-1,3),axis=0).astype(np.uint8)
        else:
            crop = pixels[y0:y1, x0:x1]
            gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
            # The mask is confined to the editable text region and foreground ink.
            threshold = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                               cv2.THRESH_BINARY_INV, 15, 9)
            if float(gray.mean()) < 100:
                threshold = cv2.bitwise_not(threshold)
                solid_ink = (gray > 145).astype(np.uint8) * 255
            else:
                solid_ink = (gray < 110).astype(np.uint8) * 255
            # Adaptive threshold alone leaves the centers of solid hearts/bold glyphs.
            threshold = cv2.bitwise_or(threshold, solid_ink)
            if region.get('text_polygons'):
                detected=np.zeros_like(threshold)
                for polygon in region['text_polygons']:
                    points=np.asarray(polygon,np.float32)-[x0,y0]
                    cv2.fillPoly(detected,[np.round(points).astype(np.int32)],255)
                reach=7 if (cleanup or {}).get('strength')=='enhanced' else 3
                threshold=cv2.bitwise_and(threshold,cv2.dilate(detected,np.ones((reach,reach),np.uint8)))
            if (cleanup or {}).get('strength')=='enhanced':
                threshold=cv2.dilate(threshold,np.ones((5,5),np.uint8))
            if owner:
                # Confirmed monochrome text backgrounds need no artwork synthesis.
                # Flat replacement prevents grey smears around large bold lettering.
                erase=cv2.dilate(threshold,np.ones((3,3),np.uint8))*allowed
                background=(gray>=220) if boundaries.tone(owner)=='light' else (gray<=80)
                samples=crop[(allowed>0)&background]
                color=np.median(samples,axis=0).astype(np.uint8) if len(samples) else (255 if boundaries.tone(owner)=='light' else 0)
                crop[erase>0]=color
                continue
            # Work on a padded crop to avoid processing the entire long-strip image per region.
            pad = 20
            left, top, right, bottom = max(0, x0-pad), max(0, y0-pad), min(width, x1+pad), min(height, y1+pad)
            mask = np.zeros((bottom-top,right-left), dtype=np.uint8)
            mask[y0-top:y1-top,x0-left:x1-left] = cv2.dilate(threshold, np.ones((3, 3), np.uint8))*allowed
            pixels[top:bottom, left:right] = cv2.inpaint(pixels[top:bottom, left:right], mask, 3, cv2.INPAINT_TELEA)
    image = Image.fromarray(pixels)
    if cleanup:
        from .cleanup import repair_background
        image=repair_background(original,image,cleanup,boundaries,offset=boundary_offset,backend=backend,gpu=gpu)
    for ink,x,y in plans:image.paste(ink,(x,y),ink)
    return image


def render_page(source: Path, regions: list[dict], destination: Path, font_path: str, scope="all", layout="auto", manual_selections=None, style="reference", *, backend=None,settings=None,cleanup=None,clean_only=False,incremental=None):
    from .cleanup import validate_cleanup
    cleanup=validate_cleanup(cleanup)
    cleanup.setdefault('strength',getattr(settings,'comic_cleanup_strength','standard'))
    if settings is not None:
        from .text_replace import apply_global_replacements
        apply_global_replacements({'regions':regions,'manual_selections':manual_selections or []},settings)
    if any(r.get('processing_engine')=='mtu' for r in regions):
        from .mtu_bridge import render_mtu_page
        return render_mtu_page(source,regions,destination,font_path,scope,layout,manual_selections,backend,cleanup=cleanup,clean_only=clean_only,incremental=incremental)
    original = load_image(source)
    boundaries=PageBoundaries(original,[r['box'] for r in regions])
    image = render_image(original, regions, font_path, scope, layout,style,boundaries=boundaries,cleanup=cleanup,clean_only=clean_only,backend=backend,gpu=getattr(settings,'gpu',True))
    for selection in manual_selections or []:
        x,y,w,h = selection["box"]
        patch = render_image(original.crop((x,y,x+w,y+h)), selection["regions"], font_path, "all", layout,style,boundaries=boundaries,boundary_offset=(x,y),cleanup=cleanup,clean_only=clean_only,backend=backend,gpu=getattr(settings,'gpu',True))
        if selection.get('recognition_box'):
            from .layout_area import composite_selection,covered_by_selection
            previous=[(r,(0,0)) for r in regions if covered_by_selection(r,selection)]
            image=composite_selection(original,image,patch,selection,previous)
        else:image.paste(patch, (x,y))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.png")
    image.save(temporary, format="PNG")
    temporary.replace(destination)
    return destination


def merge_boxes(boxes: list, width: int, height: int, image=None) -> list[dict]:
    groups = []
    boundaries=(PageBoundaries(image,[[float(np.asarray(p)[:,0].min()),float(np.asarray(p)[:,1].min()),
                                     float(np.ptp(np.asarray(p)[:,0])),float(np.ptp(np.asarray(p)[:,1]))] for p in boxes])
                if image is not None else None)
    members=[];owners=[]
    for polygon in boxes:
        coordinates = np.asarray(polygon)
        x0, y0 = coordinates.min(axis=0)
        x1, y1 = coordinates.max(axis=0)
        groups.append([max(0, int(x0)-3), max(0, int(y0)-3), min(width, int(x1)+3), min(height, int(y1)+3)])
        box=[max(0,int(x0)),max(0,int(y0)),max(1,int(x1-x0)),max(1,int(y1-y0))]
        owner=boundaries.owner(box) if boundaries else None
        if owner:
            clipped=boundaries.recognition_box(box,owner)
            a,b,c,d=clipped
            groups[-1]=[a,b,a+c,b+d]
        owners.append(('balloon',owner) if owner else ('panel',boundaries.panel(box)) if boundaries else None)
        members.append([np.asarray(polygon).tolist()])
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            if changed:
                break
            a = groups[i]
            for j in range(i+1, len(groups)):
                b = groups[j]
                gap = 8
                if owners[i]==owners[j] and a[0] <= b[2]+gap and b[0] <= a[2]+gap and a[1] <= b[3]+gap and b[1] <= a[3]+gap:
                    groups[i] = [min(a[0],b[0]), min(a[1],b[1]), max(a[2],b[2]), max(a[3],b[3])]
                    groups.pop(j)
                    owners.pop(j)
                    members[i].extend(members.pop(j))
                    changed = True
                    break
    # Top-to-bottom bands; Japanese columns inside a band are right-to-left.
    ordered=sorted(zip(groups,members),key=lambda value:(int(value[0][1]/120),-value[0][0]))
    regions = []
    for (x0,y0,x1,y1),polygons in ordered:
        vertical, horizontal = 0, 0
        sizes=[]
        for polygon in boxes:
            coordinates = np.asarray(polygon)
            a,b = coordinates.min(axis=0), coordinates.max(axis=0)
            if a[0] >= x0 and a[1] >= y0 and b[0] <= x1 and b[1] <= y1:
                area = max(1, (b[0]-a[0])*(b[1]-a[1]))
                sizes.append(min(b[0]-a[0],b[1]-a[1]))
                if b[1]-a[1] > (b[0]-a[0])*1.15:
                    vertical += area
                else:
                    horizontal += area
        regions.append({"box": [x0,y0,x1-x0,y1-y0], "text": "", "translation": "", "font_size": 0,
                        "mode": "inpaint", "color": "#111111",
                        "source_font_size": round(float(np.median(sizes))) if sizes else 0,
                        **({"text_polygons":polygons} if image is not None else {}),
                        # Detectors sometimes report individual glyph rows as
                        # horizontal boxes even inside a tall vertical column.
                        "source_orientation": "vertical" if vertical > horizontal or y1-y0 > (x1-x0)*1.3 else "horizontal"})
    return regions
