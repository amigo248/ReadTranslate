"""Human-readable timestamps for the task and request history."""
from datetime import datetime,timedelta,timezone

DISPLAY_TIMEZONE=timezone(timedelta(hours=8))

def format_timestamp(value,empty='未记录'):
    if value is None:return empty
    try:return datetime.fromtimestamp(float(value),DISPLAY_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError,TypeError,OverflowError,OSError):return empty
