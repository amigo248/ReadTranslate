"""An isolated, line-based OCR process; heavy model imports never block the UI."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback
from pathlib import Path


def serve():
    detector, recognizer = None, None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("quit"):
                break
            # Third-party model logs belong on stderr, not on the JSON protocol stream.
            with contextlib.redirect_stdout(sys.stderr):
                from PIL import Image
                from .images import load_image, merge_boxes
                from .config import data_root, write_json
                os.environ['HF_HOME'] = str(data_root() / "models" / "huggingface")
                os.environ['HF_HUB_CACHE'] = str(data_root() / 'models/huggingface/hub')
                os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
                os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
                if detector is None:
                    from rapidocr import RapidOCR
                    detector = RapidOCR(params={"Global.use_cls": False, "Global.use_rec": False,
                                                "Global.model_root_dir": str(data_root() / "models" / "rapidocr")})
                image = load_image(Path(request["path"]))
                from .comic_regions import annotate_regions,PageBoundaries,white_frame_boxes,prioritize_white_regions
                existing=request.get('existing_regions')
                forced=request.get('forced_boxes')
                boxes = []
                tile_height = 1800
                overlap = 160
                for top in ([] if existing is not None or forced is not None else range(0, image.height, tile_height - overlap)):
                    tile = image.crop((0, top, image.width, min(image.height, top + tile_height)))
                    import numpy as np
                    output = detector(np.asarray(tile), use_det=True, use_cls=False, use_rec=False)
                    if output.boxes is not None:
                        for polygon in output.boxes:
                            shifted = [[float(x), float(y)+top] for x,y in polygon]
                            if not any(abs(min(p[0] for p in existing)-min(p[0] for p in shifted)) < 8 and
                                       abs(min(p[1] for p in existing)-min(p[1] for p in shifted)) < 12 for existing in boxes):
                                boxes.append(shifted)
                    if top + tile_height >= image.height:
                        break
                detected=existing if existing is not None else merge_boxes(boxes,image.width,image.height,image=image)
                extra=[] if forced is not None else white_frame_boxes(image,detected,missing_frames_only=existing is not None)
                for x,y,w,h in extra:boxes.append([[x,y],[x+w,y],[x+w,y+h],[x,y+h]])
                regions = merge_boxes(boxes, image.width, image.height,image=image)
                if forced is not None:
                    regions=[{'box':box,'text':'','translation':'','font_size':0,'mode':'inpaint','color':'#111111'} for box in forced]
                annotate_regions(image, regions)
                if regions:
                    if recognizer is None:
                        import torch
                        from manga_ocr import MangaOcr
                        use_gpu = request.get("gpu", True) and torch.cuda.is_available()
                        cached=data_root()/'models/huggingface/hub/models--kha-white--manga-ocr-base'
                        reference=cached/'refs/main'
                        snapshot=cached/'snapshots'/reference.read_text('utf-8').strip() if reference.is_file() else None
                        if snapshot is None or not (snapshot/'config.json').is_file():
                            raise ValueError('内置 Manga OCR 模型缺失，请恢复完整 data/models 文件夹。')
                        recognizer = MangaOcr(pretrained_model_name_or_path=str(snapshot),force_cpu=not use_gpu)
                    boundaries=PageBoundaries(image,[r['box'] for r in regions])
                    for region in prioritize_white_regions(image,regions):
                        x,y,w,h = region["box"]
                        crop=image.crop((x,y,x+w,y+h))
                        owner=boundaries.owner(region['box'])
                        if owner:
                            import numpy as np
                            pixels=np.asarray(crop).copy()
                            pixels[boundaries.recognition_mask(region['box'],owner)==0]=0 if boundaries.tone(owner)=='dark' else 255
                            crop=Image.fromarray(pixels)
                        if boundaries.tone(owner)=='dark':
                            from PIL import ImageOps
                            crop=ImageOps.invert(crop)
                        region["text"] = recognizer(crop)
                if existing is not None:regions=existing+regions
                write_json(Path(request["output"]), {"regions": regions, "width": image.width, "height": image.height,"boundary_version":3,"white_scan_version":1})
            print(json.dumps({"ok": True, "regions": len(regions)}), flush=True)
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    serve()
