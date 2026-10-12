from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QObject,QThread,Signal


class Task(QThread):
    progress = Signal(int,int,str)
    succeeded = Signal(object)
    failed = Signal(str)
    paused = Signal(str)

    def __init__(self,function,parent=None):
        super().__init__(parent)
        self.function = function
        self.cancelled = threading.Event()

    def run(self):
        try:
            result = self.function(self.progress.emit,self.cancelled.is_set)
            self.succeeded.emit(result)
        except InterruptedError as error:
            self.paused.emit(str(error))
        except Exception as error:
            traceback.print_exc()
            self.failed.emit(str(error))

    def cancel(self):
        self.cancelled.set()
