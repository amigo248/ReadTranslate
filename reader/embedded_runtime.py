"""Resolve owned, relocatable translation assets and isolate child processes."""
from pathlib import Path
import contextlib
import os
import sys
from .config import app_root, data_root

FONT_CHOICES=(('内置黑体','builtin'),('内置微软雅黑','builtin-yahei'),
              ('内置 Arial Unicode','builtin-unicode'),('MS Gothic（日文字体）','owned-font:msgothic.ttc'),
              ('Anime Ace 3（西文）','owned-font:anime_ace_3.ttf'),('Anime Ace（西文）','owned-font:anime_ace.ttf'),
              ('Comic Shanns 2（西文）','owned-font:comic shanns 2.ttf'))

def manga_root():
    return app_root()/'engines/manga'


def resolve_font(value):
    fonts={'':'simhei.ttf','builtin':'simhei.ttf','内置中文字体':'simhei.ttf',
           'builtin-yahei':'msyh.ttc','builtin-unicode':'Arial-Unicode-Regular.ttf'}
    if value in fonts:
        requested=manga_root()/'fonts'/fonts[value]
        portable=manga_root()/'fonts/NotoSansCJKsc-Regular.otf'
        return str(requested if requested.is_file() or not portable.is_file() else portable)
    if str(value).startswith('owned-font:'):
        name=str(value).removeprefix('owned-font:')
        if name not in {v.removeprefix('owned-font:') for _,v in FONT_CHOICES if v.startswith('owned-font:')}:
            raise ValueError('内置字体标记无效，请重新选择字体')
        return str(manga_root()/'fonts'/name)
    if str(value).startswith('user-font:'):
        name=str(value).removeprefix('user-font:')
        if not name or Path(name).name!=name or '/' in name or '\\' in name:
            raise ValueError('导入的字体路径无效，请重新选择字体文件')
        return str(data_root()/'fonts'/name)
    return str(value)


def import_font(source):
    """Keep a validated copy in the reader's data, independent of the source file."""
    import hashlib
    import shutil
    from PIL import ImageFont
    source=Path(source)
    if source.suffix.lower() not in ('.ttf','.ttc','.otf'):
        raise ValueError('请选择 TTF、TTC 或 OTF 字体文件')
    try:ImageFont.truetype(str(source),24)
    except (OSError,ValueError) as error:raise ValueError('无法读取该字体文件，请选择有效字体') from error
    with source.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()[:16]
    destination=data_root()/'fonts'/f'{source.stem[:40]}-{digest}{source.suffix.lower()}'
    destination.parent.mkdir(parents=True,exist_ok=True)
    if not destination.exists():shutil.copy2(source,destination)
    return 'user-font:'+destination.name

def child_environment():
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(('LLAMA_ARG_', 'GGML_')) or key in (
            'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'PYTHONHOME', 'PYTHONPATH'):
            env.pop(key, None)
    if os.name == 'nt':
        env['PATH'] = os.pathsep.join([str(Path(os.environ.get('SystemRoot', 'C:/Windows'))/'System32'),
                                      os.environ.get('SystemRoot', 'C:/Windows')])
    return env

@contextlib.contextmanager
def isolated_dll_search():
    directory = None
    if os.name == 'nt' and getattr(sys, 'frozen', False):
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        ctypes.windll.kernel32.GetDllDirectoryW(len(buffer), buffer)
        directory = buffer.value
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        yield
    finally:
        if directory is not None:
            ctypes.windll.kernel32.SetDllDirectoryW(directory)


# Public portable builds carry OFL fonts; existing installations retain their
# established font choices. No dependency on fonts installed on the computer.
if (manga_root()/'fonts/NotoSansCJKsc-Regular.otf').is_file() and not (manga_root()/'fonts/simhei.ttf').is_file():
    FONT_CHOICES=(('Noto Sans CJK（中日文）','builtin'),
                  ('Noto Sans CJK Bold（中日文粗体）','owned-font:NotoSansCJKsc-Bold.otf'))
