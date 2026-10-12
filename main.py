from __future__ import annotations

import os
import sys


def start_gui():
    from reader.config import data_root
    log=(data_root()/"application.log").open("a",encoding="utf-8",buffering=1)
    if sys.stdout is None:sys.stdout=log
    if sys.stderr is None:sys.stderr=log
    try:
        from reader.app import run
        return run()
    except Exception:
        import traceback
        traceback.print_exc(file=log)
        if os.name=="nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,"程序未能启动，请查看 data/application.log。","阅译",0x10)
        return 1


if __name__ == "__main__":
    # PyInstaller subprocesses enter the OCR protocol without opening a second GUI.
    if "--ocr-worker" in sys.argv:
        from reader.ocr_worker import serve
        serve()
    else:
        raise SystemExit(start_gui())
