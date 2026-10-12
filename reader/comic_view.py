"""A continuous manga canvas with viewport loading and a bounded image cache."""
from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
import math

from PySide6.QtCore import QObject, QRunnable, QRectF, QSize, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter,QPalette
from PySide6.QtWidgets import QWidget


class ImageResults(QObject):
    ready = Signal(int, int, QImage, str)


class LoadPage(QRunnable):
    def __init__(self, results, generation, index, path, size):
        super().__init__()
        self.results, self.generation, self.index = results, generation, index
        self.path, self.size = path, size

    def run(self):
        try:
            reader = QImageReader(str(self.path))
            reader.setAutoTransform(True)
            native = reader.size()
            factor = max(self.size.width(), self.size.height()) / max(1, native.width(), native.height())
            reader.setScaledSize(QSize(max(1, round(native.width() * factor)), max(1, round(native.height() * factor))))
            image = reader.read()
            error = reader.errorString() if image.isNull() else ""
        except Exception as exc:
            image, error = QImage(), str(exc)
        self.results.ready.emit(self.generation, self.index, image, error)


class ContinuousComicCanvas(QWidget):
    MAX_CACHE_BYTES = 96 * 1024 * 1024
    MAX_CACHE_PAGES = 8
    GAP = 8

    def __init__(self, area):
        super().__init__()
        self.area = area
        self.paths, self.sizes, self.rects, self.starts = [], [], [], []
        self.signature = None
        self.fit = "width"
        self.cache = OrderedDict()
        self.errors, self.pending = {}, set()
        self.todo = []
        self.generation = 0
        self.results = ImageResults(self)
        self.results.ready.connect(self.page_loaded, Qt.ConnectionType.QueuedConnection)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        area.verticalScrollBar().valueChanged.connect(self.refresh_visible)
        area.horizontalScrollBar().valueChanged.connect(self.refresh_visible)

    def set_pages(self, paths, fit):
        signature = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
        if signature != self.signature:
            self.signature = signature
            self.paths = list(paths)
            self.sizes = []
            for path in paths:
                # Image dimensions are read from headers, without decoding the book.
                from PIL import Image
                with Image.open(path) as image:
                    width, height = image.size
                    if image.getexif().get(274) in {5, 6, 7, 8}:
                        width, height = height, width
                self.sizes.append((max(1, width), max(1, height)))
            self.invalidate()
        self.fit = fit
        self.adjust_canvas()

    def invalidate(self):
        self.generation += 1
        self.pool.clear()
        self.cache.clear()
        self.errors.clear()
        self.pending.clear()
        self.todo.clear()

    def adjust_canvas(self):
        if not self.paths:
            return
        viewport = self.area.viewport()
        width, height = max(100, viewport.width()), max(100, viewport.height())
        dimensions = []
        for w, h in self.sizes:
            factor = 1 if self.fit == "actual" else width / w
            if self.fit == "page":
                factor = min(factor, max(100, height - self.GAP) / h)
            dimensions.append((max(1, round(w * factor)), max(1, round(h * factor))))
        canvas_width = max(width, max(w for w, h in dimensions))
        rects, starts, y = [], [], 0
        for w, h in dimensions:
            starts.append(y)
            rects.append(QRectF((canvas_width - w) / 2, y, w, h))
            y += h + self.GAP
        changed = [(r.width(), r.height()) for r in rects] != [(r.width(), r.height()) for r in self.rects]
        self.rects, self.starts = rects, starts
        if changed:
            self.invalidate()
        self.setFixedSize(canvas_width, max(height, y - self.GAP))
        self.refresh_visible()

    def position(self, value):
        if not self.rects:
            return 0, 0.0
        index = max(0, min(len(self.rects) - 1, bisect_right(self.starts, value) - 1))
        rect = self.rects[index]
        return index, max(0.0, min(1.0, (value - rect.top()) / rect.height()))

    def scroll_value(self, index, fraction=0):
        if not self.rects:
            return 0
        rect = self.rects[max(0, min(index, len(self.rects) - 1))]
        return round(rect.top() + rect.height() * fraction)

    def visible_indexes(self, margin=0):
        if not self.rects:
            return []
        top = self.area.verticalScrollBar().value()
        bottom = top + self.area.viewport().height()
        first = max(0, bisect_right(self.starts, top) - 1 - margin)
        last = min(len(self.rects), bisect_right(self.starts, bottom) + margin)
        return list(range(first, last))

    def refresh_visible(self, *args):
        if not self.isVisible():
            return
        indexes = self.visible_indexes(1)
        # Load the visible page before its neighbours after a large jump.
        visible = self.visible_indexes()
        indexes.sort(key=lambda i: (i not in visible, abs(i - (visible[0] if visible else 0))))
        self.todo = indexes
        self.schedule_loads()
        self.trim_cache()
        self.update()

    def schedule_loads(self):
        while self.todo and len(self.pending) < 4:
            index = self.todo.pop(0)
            if index in self.cache or index in self.pending or index in self.errors:
                continue
            rect = self.rects[index]
            width, height = self.sizes[index]
            factor = min(1.0, rect.width() * self.devicePixelRatioF() / width,
                         4096 / max(width, height), math.sqrt(8_000_000 / (width * height)))
            size = QSize(max(1, round(width * factor)), max(1, round(height * factor)))
            self.pending.add(index)
            self.pool.start(LoadPage(self.results, self.generation, index, self.paths[index], size))

    @Slot(int, int, QImage, str)
    def page_loaded(self, generation, index, image, error):
        if generation != self.generation:
            return
        self.pending.discard(index)
        if not self.isVisible() or index not in self.visible_indexes(1):
            self.schedule_loads()
            return
        if error:
            self.errors[index] = error
        else:
            self.cache[index] = image
            self.cache.move_to_end(index)
            self.trim_cache()
        self.schedule_loads()
        self.update()

    @property
    def cache_bytes(self):
        return sum(image.sizeInBytes() for image in self.cache.values())

    def trim_cache(self):
        visible = set(self.visible_indexes())
        while self.cache and (len(self.cache) > self.MAX_CACHE_PAGES or self.cache_bytes > self.MAX_CACHE_BYTES):
            victim = next((i for i in self.cache if i not in visible), next(iter(self.cache)))
            del self.cache[victim]

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_visible()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), self.palette().color(QPalette.ColorRole.Window))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(self.palette().color(QPalette.ColorRole.Text))
        for index in self.visible_indexes():
            rect = self.rects[index]
            image = self.cache.get(index)
            if image is not None:
                painter.drawImage(rect, image)
            else:
                painter.fillRect(rect, self.palette().color(QPalette.ColorRole.Base))
                message = "图片无法加载" if index in self.errors else "正在加载…"
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, message)

    def release(self):
        self.invalidate()
