"""Explicit local repair of untouched legacy colored pages, preserving old runs."""
from copy import deepcopy
import os,time,uuid
import numpy as np
from PIL import Image
from .config import write_json
from .colorization import (available_color,read_record,pointer_path,source_path,original_path)
from .color_geometry import normalize_result,GEOMETRY_VERSION
from .upscaling import fingerprint


def repair_legacy(library,book,index,edition):
    saved=read_record(library,book,index,edition)
    if not saved or not available_color(library,book,index,edition):return {'repaired':False,'reason':'not-current'}
    record,folder=saved;pointer=pointer_path(library,book,index,edition);before=pointer.read_bytes()
    if record.get('geometry_version')==GEOMETRY_VERSION:return {'repaired':False,'reason':'already-normalized'}
    if record.get('workflow')!='colorize_only' or record.get('refresh')!='generated':return {'repaired':False,'reason':'reviewed-or-legacy-workflow'}
    for name,key in [('result.png','output_hash'),('source.png','snapshot_hash'),('protected.png','mask_hash'),('base.png','base_hash')]:
        if fingerprint(folder/name)!=record[key]:return {'repaired':False,'reason':'invalid-cache'}
    if record['base_hash']!=record['output_hash']:return {'repaired':False,'reason':'manually-edited'}
    with Image.open(folder/'protected.png') as mask:
        if np.any(np.asarray(mask)):return {'repaired':False,'reason':'manual-text-mask'}
    source=source_path(library,book,index,edition);source_hash=fingerprint(source)
    original_hash=fingerprint(original_path(library,book,index))
    if source_hash!=record['source_hash'] or original_hash!=record['original_hash']:return {'repaired':False,'reason':'concurrent-change'}
    with Image.open(source) as page:page=page.convert('RGB').copy()
    with Image.open(folder/'base.png') as model:model=model.convert('RGB').copy()
    output,geometry=normalize_result(model,page.size,source=page)
    if geometry['method']!='verified_registration':return {'repaired':False,'reason':'insufficient-evidence'}
    alignment=geometry['registration'];ratio=alignment['x_scale']/alignment['y_scale']
    if abs(ratio-1)<.025:return {'repaired':False,'reason':'no-material-aspect-error'}
    new=library.path(book)/'colored/runs'/uuid.uuid4().hex/f'{index:06d}';new.mkdir(parents=True,exist_ok=True)
    # This old base is not the original API response; name/metadata say so.
    model.save(new/'previous-base.png');output.save(new/'base.png');output.save(new/'result.png')
    page.save(new/'source.png');Image.new('L',page.size).save(new/'protected.png')
    fresh=deepcopy(record)
    fresh.update(run=new.relative_to(library.path(book)/'colored').as_posix(),geometry=geometry,geometry_version=GEOMETRY_VERSION,
                 geometry_repaired_from=record['run'],geometry_repaired_at=time.time(),refresh='geometry_repaired',
                 model_file='previous-base.png',model_raw_available=False,model_hash=fingerprint(new/'previous-base.png'),
                 output_hash=fingerprint(new/'result.png'),base_hash=fingerprint(new/'base.png'),
                 snapshot_hash=fingerprint(new/'source.png'),mask_hash=fingerprint(new/'protected.png'))
    write_json(new/'record.json',fresh)
    # A GUI review made during verification wins; never overwrite that change.
    if (pointer.read_bytes()!=before or fingerprint(source)!=source_hash or fingerprint(original_path(library,book,index))!=original_hash):
        return {'repaired':False,'reason':'concurrent-change','unpublished_run':str(new)}
    staged=pointer.with_name(pointer.name+'.'+uuid.uuid4().hex+'.new')
    try:
        write_json(staged,fresh)
        if pointer.read_bytes()!=before:return {'repaired':False,'reason':'concurrent-change','unpublished_run':str(new)}
        os.replace(staged,pointer)
    finally:staged.unlink(missing_ok=True)
    return {'repaired':True,'page':index+1,'edition':edition,'old_run':record['run'],'new_run':fresh['run'],
            'geometry':geometry,'paid_requests':0,'old_files_preserved':True,'source_unchanged':fingerprint(source)==source_hash}
