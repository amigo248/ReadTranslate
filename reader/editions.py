"""Resolve the three independent reader choices without starting any work."""
EDITIONS = ('original', 'translated', 'upscaled_original', 'upscaled_translated')
LABELS = dict(zip(EDITIONS, ('原版', '中文版', '原版超分', '中文版超分')))


def edition_name(translated=False, upscaled=False):
    return ('upscaled_' if upscaled else '') + ('translated' if translated else 'original')


def display_page(library, book, index, edition, colored=False):
    """Return a readable fallback and an explicit notice if an edition is missing."""
    from .pipeline import translated_path
    from .upscaling import available_edition
    if edition not in EDITIONS:
        raise ValueError('阅读版本无效')
    original = library.path(book)/library.manifest(book)['units'][index]['path']
    translated = translated_path(library, book, index)
    wants_translation = edition.endswith('translated')
    basic = translated if wants_translation and translated.is_file() else original
    missing = []
    if colored:
        from .colorization import available_color
        color = available_color(library, book, index, edition)
        if color:
            return color, ''
        missing.append('对应彩色版未生成或已失效')
    path = basic
    actual = '中文版' if basic == translated else '原版'
    if edition.startswith('upscaled_'):
        enhanced = available_edition(library, book, index, edition.removeprefix('upscaled_'))
        if enhanced:
            path, actual = enhanced, LABELS[edition]
        else:
            missing.append('对应超分版未生成或已失效')
    if wants_translation and not translated.is_file():
        missing.append('尚未生成译图')
    return path, '；'.join(missing) + f'，当前显示{actual}' if missing else ''
