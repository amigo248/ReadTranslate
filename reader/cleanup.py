"""Persistent brush masks, background repair and transactional page cleanup."""
import hashlib
import math
import os
import shutil
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image


def validate_cleanup(value):
    value = deepcopy(value or {})
    if value.get('strength', 'standard') not in ('standard', 'enhanced'):
        raise ValueError('残字清理强度无效')
    strokes = value.setdefault('strokes', [])
    scale=value.get('coordinate_scale',1)
    if scale not in (1,2,3,4):raise ValueError('清除范围缩放倍率无效')
    if not isinstance(strokes, list) or len(strokes) > 2000:
        raise ValueError('清除笔画过多，请分次处理')
    for stroke in strokes:
        if stroke.get('mode', 'auto') not in ('auto', 'white', 'black', 'inpaint'):
            raise ValueError('背景修复方式无效')
        size = stroke.get('size', 20)
        if not isinstance(size, (int, float)) or not math.isfinite(size) or not 2 <= size <= 160*scale:
            raise ValueError('笔刷大小应为 2–160 像素')
        points = stroke.get('points', [])
        if not points or len(points) > 20000:
            raise ValueError('清除笔画坐标无效')
        if any(len(p) != 2 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in p) for p in points):
            raise ValueError('清除笔画坐标无效')
    return value


def stroke_masks(image, cleanup, boundaries, offset=(0, 0)):
    """Clip each page-coordinate stroke to its original compartment, never its rim."""
    cleanup = validate_cleanup(cleanup)
    width, height = image.size
    ox, oy = offset
    entries = []
    for stroke in cleanup['strokes']:
        points = np.asarray(stroke['points'], dtype=float) - [ox, oy]
        radius = int(math.ceil(stroke.get('size', 20) / 2))
        left = max(0, int(math.floor(points[:, 0].min())) - radius)
        top = max(0, int(math.floor(points[:, 1].min())) - radius)
        right = min(width, int(math.ceil(points[:, 0].max())) + radius + 1)
        bottom = min(height, int(math.ceil(points[:, 1].max())) + radius + 1)
        if right <= left or bottom <= top:
            continue
        box = [left + ox, top + oy, right - left, bottom - top]
        anchor = stroke['points'][0]
        reach=max(8,radius+6)
        owner = boundaries.owner([int(anchor[0]) - reach, int(anchor[1]) - reach, reach*2+1, reach*2+1])
        if owner:
            safe = boundaries.shape_crop(owner, box)
        else:
            safe, _ = boundaries.allowed(box)
        ink = np.zeros(safe.shape, np.uint8)
        local = np.round(points - [left, top]).astype(np.int32)
        if len(local) > 1:
            cv2.polylines(ink, [local], False, 255, max(2, round(stroke.get('size', 20))))
        for point in (local[0], local[-1]):
            cv2.circle(ink, tuple(point), radius, 255, -1)
        ink[safe == 0] = 0
        if ink.any():
            mask = np.zeros((height, width), np.uint8)
            mask[top:bottom, left:right] = ink
            entries.append((stroke.get('mode', 'auto'), mask))
    return entries


def brush_mask(image, cleanup, boundaries, offset=(0, 0)):
    mask = np.zeros((image.height, image.width), np.uint8)
    for _, ink in stroke_masks(image, cleanup, boundaries, offset):
        mask |= ink
    return mask


def background_color(original, mask):
    """Require a dominant flat local background; patterned areas use LaMa."""
    ys, xs = np.where(mask > 0)
    if not len(xs):
        return None
    x0, y0 = max(0, xs.min() - 12), max(0, ys.min() - 12)
    x1, y1 = min(mask.shape[1], xs.max() + 13), min(mask.shape[0], ys.max() + 13)
    selected = mask[y0:y1, x0:x1]
    ring = (cv2.dilate(selected, np.ones((21, 21), np.uint8)) > 0) & (selected == 0)
    pixels = np.asarray(original)[y0:y1, x0:x1][ring]
    if len(pixels) < 12:
        return None
    buckets = pixels.astype(np.int32) // 12
    _, labels, counts = np.unique(buckets, axis=0, return_inverse=True, return_counts=True)
    winner = int(counts.argmax())
    if counts[winner] / len(pixels) < .88:
        return None
    color = np.median(pixels[labels == winner], axis=0).astype(np.uint8)
    if np.quantile(np.abs(pixels.astype(float) - color).max(axis=1), .90) > 10:
        return None
    return color


def repair_background(original, cleaned, cleanup, boundaries, *, offset=(0, 0), backend=None, gpu=True):
    entries = stroke_masks(original, cleanup, boundaries, offset)
    if not entries:
        return cleaned
    pixels = np.asarray(cleaned.convert('RGB')).copy()
    inpaint = np.zeros(pixels.shape[:2], np.uint8)
    union = np.zeros_like(inpaint)
    fills = []
    occupied=np.zeros_like(inpaint)
    for mode, mask in reversed(entries):
        effective=mask.copy();effective[occupied>0]=0
        occupied|=mask;mask=effective
        if not mask.any():continue
        union |= mask
        color = np.array([255] * 3 if mode == 'white' else [0] * 3, np.uint8) if mode in ('white', 'black') else background_color(original, mask) if mode == 'auto' else None
        if color is None:
            inpaint |= mask
        else:
            fills.append((mask, color))
    if inpaint.any():
        from .mtu_bridge import MTUProcess
        with tempfile.TemporaryDirectory(prefix='residual-repair-') as folder:
            root = Path(folder)
            own = backend is None
            process = backend or MTUProcess(SimpleNamespace(root=root))
            if own:process.gpu=gpu
            try:
                Image.fromarray(pixels).save(root / 'base.png')
                Image.fromarray(inpaint).save(root / 'mask.png')
                process.request('clean', path=str(root / 'base.png'), mask=str(root / 'mask.png'),
                                output=str(root / 'repaired.png'), gpu=getattr(process, 'gpu', gpu), fills=[])
                with Image.open(root / 'repaired.png') as image:
                    result = np.asarray(image.convert('RGB'))
                if result.shape != pixels.shape:
                    raise ValueError('背景修复尺寸错误，旧译图已保留')
                pixels[inpaint > 0] = result[inpaint > 0]
            finally:
                if own:
                    process.close()
    for mask, color in fills:
        pixels[mask > 0] = color
    assert np.array_equal(pixels[union == 0], np.asarray(cleaned.convert('RGB'))[union == 0])
    return Image.fromarray(pixels)


def file_hash(path):
    if not path.exists():
        return None
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def page_state(library, book_id, index,review_key=None):
    from .pipeline import edit_path, translated_path
    source = library.path(book_id) / library.manifest(book_id)['units'][index]['path']
    if review_key:
        from .edition_review import data_path,valid_image,folder
        image=valid_image(library,book_id,index,review_key)
        return [file_hash(source),file_hash(data_path(library,book_id,index,review_key)),
                file_hash(image) if image else None,file_hash(folder(library,book_id,index,review_key)/'record.json')]
    from .upscaling import edition_path
    enhanced=edition_path(library,book_id,index,'translated')
    return [file_hash(p) for p in (source, edit_path(library, book_id, index), translated_path(library, book_id, index),
                                 enhanced, enhanced.with_suffix('.json'))]


def preview_cleanup(library, settings, book_id, index, data, cleanup, directory, progress=lambda *_: None, cancel=lambda: False,*,review_key=None):
    from .images import render_page
    from .config import write_json
    data = deepcopy(data)
    cleanup = validate_cleanup(cleanup)
    data['residual_cleanup'] = cleanup
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if review_key:
        from .edition_review import stage_review
        before=page_state(library,book_id,index,review_key)
        outputs,_,_=stage_review(library,book_id,index,review_key,data,settings,directory/'final',cancel=cancel)
        clean_outputs,_,_=stage_review(library,book_id,index,review_key,data,settings,directory/'clean',cancel=cancel,clean_only=True)
        shutil.copy2(outputs[0][1],directory/'translated.png')
        shutil.copy2(clean_outputs[0][1],directory/'clean.png')
        if page_state(library,book_id,index,review_key)!=before:raise ValueError('页面已修改，请重新预览')
        return {'directory':str(directory),'review_key':review_key,'before':before,
                'files':{p.relative_to(directory).as_posix():file_hash(p) for _,p in outputs},
                'outputs':[(str(target),p.relative_to(directory).as_posix()) for target,p in outputs]}
    before = page_state(library, book_id, index)
    source = library.path(book_id) / library.manifest(book_id)['units'][index]['path']
    native = any(r.get('processing_engine') == 'mtu' for r in data.get('regions', []))
    backend = None
    if native:
        from .mtu_bridge import MTUProcess
        backend = MTUProcess(SimpleNamespace(root=directory), cancel)
        backend.gpu = settings.gpu
    try:
        for n, (name, clean_only) in enumerate((('clean.png', True), ('translated.png', False))):
            if cancel():
                raise InterruptedError('清理已取消，原校对和译图已保留')
            progress(n, 2, '准备清理底图' if clean_only else '重新绘制中文译文')
            render_page(source, data.get('regions', []), directory / name, settings.font_path,
                        settings.comic_scope, settings.comic_layout, data.get('manual_selections'), settings.comic_style,
                        settings=settings, backend=backend, cleanup=cleanup, clean_only=clean_only)
        if cancel():
            raise InterruptedError('清理已取消，原校对和译图已保留')
        if page_state(library, book_id, index) != before:
            raise ValueError('页面在清理过程中发生变化，请重新打开清理窗口')
        from .page_render import stage_page_editions
        outputs=stage_page_editions(library,book_id,index,data,settings,directory/'translated.png',directory,
                                   cancel=cancel,backend=backend)
        files={name:file_hash(directory/name) for name in ('clean.png','translated.png','edits.json')}
        files.update({p.name:file_hash(p) for _,p in outputs})
        return {'directory':str(directory),'before':before,'files':files,
                'outputs':[(str(target),p.name) for target,p in outputs]}
    finally:
        if backend:
            backend.close()


def apply_cleanup(library, book_id, index, preview):
    from .pipeline import edit_path, translated_path
    from .config import write_json
    key=preview.get('review_key')
    if page_state(library, book_id, index,key) != preview['before']:
        raise ValueError('页面已修改，请重新预览后应用')
    staging = Path(preview['directory'])
    if any(file_hash(staging / name) != digest for name, digest in preview['files'].items()):
        raise ValueError('预览文件已改变，请重新预览')
    root = library.path(book_id)
    backup = root / 'backups' / ('cleanup-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    targets = [(edit_path(library, book_id, index), staging / 'edits.json'),
               (translated_path(library, book_id, index), staging / 'translated.png')]
    if preview.get('outputs'):
        from .upscaling import edition_path
        enhanced=edition_path(library,book_id,index,'translated')
        allowed={edit_path(library,book_id,index),translated_path(library,book_id,index),enhanced,enhanced.with_suffix('.json')}
        allowed.update(root/'cache/page-state'/f'{index:06d}-{kind}.json' for kind in ('normal','enhanced'))
        if key:
            from .edition_review import folder,data_path,valid_image,context
            current=valid_image(library,book_id,index,key)
            owned=folder(library,book_id,index,key)
            allowed={current,data_path(library,book_id,index,key),owned/'record.json',owned/'rendered.json',
                     context(library,book_id,index,key,staging)['state']}
            if key=='upscaled_translated':allowed.add(current.with_suffix('.json'))
            if key.startswith('colored_'):
                from .colorization import read_record,pointer_path
                _,run=read_record(library,book_id,index,key.removeprefix('colored_'))
                allowed.update((run/'record.json',pointer_path(library,book_id,index,key.removeprefix('colored_'))))
        targets=[(Path(target),staging/name) for target,name in preview['outputs']]
        if any(Path(target) not in allowed or not (staging/name).resolve().is_relative_to(staging.resolve()) for target,name in preview['outputs']):
            raise ValueError('清理预览路径无效，请重新预览')
    backup.mkdir(parents=True)
    saved = []
    for target, _ in targets:
        existed = target.exists()
        old = backup / target.relative_to(root)
        if existed:
            old.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, old)
        saved.append((target, old, existed))
    write_json(backup / 'cleanup.json', {'page': index + 1, 'files': [{'path': str(p.relative_to(root)), 'existed': existed} for p, _, existed in saved]})
    try:
        for target, source in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.cleanup-tmp')
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
    except Exception:
        for target, old, existed in saved:
            if existed:
                shutil.copy2(old, target)
            else:
                target.unlink(missing_ok=True)
        raise
    finally:
        for target, _ in targets:
            target.with_name(target.name + '.cleanup-tmp').unlink(missing_ok=True)
    return str(backup)
