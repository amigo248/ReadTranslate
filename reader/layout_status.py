"""Persist per-frame layout issues without discarding translated text."""


class TextOverflowError(ValueError):
    """The full text cannot fit inside the permitted area at the minimum size."""


def mark_overflow(region, message, box=None):
    region['render_overflow']={'message':str(message),'minimum_font_size':8,
                               'box':list(box if box is not None else region['box'])}
    region.pop('render_font_size',None)
    region.pop('render_box',None)


def clear_overflow(region):
    region.pop('render_overflow',None)


def overflow_message(region):
    issue=region.get('render_overflow')
    return issue.get('message','译文放不下，请校对后重新生成') if isinstance(issue,dict) else str(issue or '')


def overflow_count(edits):
    return sum(bool(region.get('render_overflow')) for region in edits.get('regions',[]))+sum(
        sum(bool(region.get('render_overflow')) for region in patch.get('regions',[]))
        for patch in edits.get('manual_selections',[]))
