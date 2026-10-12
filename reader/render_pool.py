"""One serialized native renderer, released when idle or the app closes."""
from contextlib import contextmanager
from threading import RLock, Timer
from types import SimpleNamespace
from pathlib import Path

_lock=RLock()
_process=None
_key=None
_timer=None
IDLE_SECONDS=45


def close_render_pool():
    global _process,_key,_timer
    with _lock:
        if _timer:
            _timer.cancel();_timer=None
        if _process:
            _process.close();_process=None
        _key=None


@contextmanager
def render_process(library,cancel,gpu):
    global _process,_key,_timer
    with _lock:
        if _timer:
            _timer.cancel();_timer=None
        key=(str(Path(library.root).resolve()),bool(gpu))
        if _key!=key or (_process and _process.process and _process.process.poll() is not None):
            close_render_pool()
        if _process is None:
            from .mtu_bridge import MTUProcess
            _process=MTUProcess(SimpleNamespace(root=library.root/'render-worker'))
            _key=key
        process=_process;process.cancel=cancel;process.gpu=gpu
        try:
            yield process
        except BaseException:
            close_render_pool()
            raise
        finally:
            if _process is process:
                process.cancel=lambda:False
                def expire():
                    with _lock:
                        if _timer is timer:close_render_pool()
                timer=Timer(IDLE_SECONDS,expire);_timer=timer;timer.daemon=True;timer.start()
