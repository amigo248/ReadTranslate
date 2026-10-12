"""Brush-based cleanup editor; previews never write the published page."""
import tempfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QImage, QPixmap, QPen, QColor, QPainterPath
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                              QComboBox, QSpinBox, QCheckBox)
from .widgets import BoxView
from .images import load_image, render_page
from .comic_regions import PageBoundaries
from .cleanup import brush_mask, preview_cleanup, apply_cleanup
from .tasks import Task


class CleanupBrushView(BoxView):
    stroke_finished = Signal(object)

    def __init__(self, parent=None):
        super().__init__()
        if parent is not None:self.setParent(parent)
        self.painting = True
        self.brush_size = 20
        self.points = None
        self.trace = None
        self.pan = None
        self.overlay = None

    def set_image(self, image):
        pixmap = QPixmap.fromImage(QImage(ImageQt(image)).copy())
        if hasattr(self, 'image_item'):
            self.image_item.setPixmap(pixmap)
        else:
            self.image_item = self.scene().addPixmap(pixmap)
            self.scene().setSceneRect(self.image_item.boundingRect())
            self.fitInView(self.image_item, Qt.AspectRatioMode.KeepAspectRatio)

    def set_mask(self, mask):
        pixels = np.zeros((*mask.shape, 4), np.uint8)
        pixels[:, :, 0] = 255
        pixels[:, :, 1] = 90
        pixels[:, :, 3] = (mask > 0).astype(np.uint8) * 110
        pixmap = QPixmap.fromImage(QImage(ImageQt(Image.fromarray(pixels))).copy())
        if self.overlay is None:
            self.overlay = self.scene().addPixmap(pixmap)
            self.overlay.setZValue(5)
        else:
            self.overlay.setPixmap(pixmap)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.pan = event.position().toPoint()
            event.accept()
            return
        point = self.mapToScene(event.position().toPoint())
        if self.painting and event.button() == Qt.MouseButton.LeftButton and self.image_item.boundingRect().contains(point):
            self.points = [[point.x(), point.y()]]
            self.path = QPainterPath(point)
            pen = QPen(QColor(255, 90, 0, 130), self.brush_size)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            self.trace = self.scene().addPath(self.path, pen)
            self.trace.setZValue(6)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.pan is not None:
            delta = event.position().toPoint() - self.pan
            self.pan = event.position().toPoint()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        if self.points is not None:
            point = self.mapToScene(event.position().toPoint())
            rect = self.image_item.boundingRect()
            x, y = max(0, min(rect.width() - 1, point.x())), max(0, min(rect.height() - 1, point.y()))
            if len(self.points) < 20000 and (abs(x-self.points[-1][0]) + abs(y-self.points[-1][1])) >= 1:
                self.points.append([x, y])
                self.path.lineTo(x, y)
                self.trace.setPath(self.path)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.pan is not None and event.button() == Qt.MouseButton.RightButton:
            self.pan = None
            event.accept()
            return
        if self.points is not None and event.button() == Qt.MouseButton.LeftButton:
            points, self.points = self.points, None
            self.scene().removeItem(self.trace)
            self.trace = None
            self.stroke_finished.emit({'points': points, 'size': self.brush_size})
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CleanupDialog(QDialog):
    def __init__(self, library, settings, book_id, index, data, parent=None,*,review_key=None):
        super().__init__(parent)
        self.library, self.settings, self.book_id, self.index = library, replace(settings), book_id, index
        self.data = deepcopy(data)
        self.review_key=review_key
        source = library.path(book_id) / library.manifest(book_id)['units'][index]['path']
        self.original = load_image(source)
        self.boundaries = PageBoundaries(self.original, [r['box'] for r in data.get('regions', [])])
        self.cleanup = deepcopy(data.get('residual_cleanup', {'strokes': [], 'strength': settings.comic_cleanup_strength}))
        self.cleanup.setdefault('strokes', [])
        self.history = []
        self.task = None
        self.closing = False
        self.applied = False
        self.preview_result = None
        self.base = self.original
        if review_key:
            from .edition_review import valid_image
            self.base=load_image(valid_image(library,book_id,index,review_key)).resize(self.original.size,Image.Resampling.LANCZOS)
        cache = library.path(book_id) / 'cache'
        cache.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='cleanup-preview-', dir=cache)
        self.setWindowTitle(f'清除残字 · 第 {index+1} 页')
        self.resize(1200, 850)
        layout = QVBoxLayout(self)
        note = QLabel('在底图上涂抹残留原字。橙色显示实际清除范围；气泡与分镜边框受保护。滚轮缩放，右键拖动查看。先预览底图与中文效果，再应用；不重新识别或翻译。')
        note.setWordWrap(True)
        layout.addWidget(note)
        toolbar = QHBoxLayout()
        self.brush = QSpinBox(); self.brush.setRange(2, 160); self.brush.setValue(20)
        self.brush.valueChanged.connect(lambda value: setattr(self.view, 'brush_size', value))
        self.paint = QCheckBox('画笔'); self.paint.setChecked(True)
        self.paint.toggled.connect(self.update_painting)
        self.mode = QComboBox()
        for label, mode in [('自动判断背景', 'auto'), ('白色填补', 'white'), ('黑色填补', 'black'), ('LaMa 修复图案 / 网点', 'inpaint')]:
            self.mode.addItem(label, mode)
        self.strength = QComboBox(); self.strength.addItem('标准清理', 'standard'); self.strength.addItem('加强清理', 'enhanced')
        self.strength.setCurrentIndex(max(0, self.strength.findData(self.cleanup.get('strength', settings.comic_cleanup_strength))))
        self.strength.currentIndexChanged.connect(self.strength_changed)
        self.display = QComboBox(); self.display.addItems(['涂抹底图', '修复后底图', '中文译图'])
        self.display.currentIndexChanged.connect(self.show_image)
        for label, widget in [('笔刷像素', self.brush), ('', self.paint), ('背景处理', self.mode), ('自动清理', self.strength), ('查看', self.display)]:
            if label: toolbar.addWidget(QLabel(label))
            toolbar.addWidget(widget)
        layout.addLayout(toolbar)
        self.view = CleanupBrushView(); self.view.set_image(self.original)
        self.view.stroke_finished.connect(self.add_stroke)
        layout.addWidget(self.view, 1)
        self.status = QLabel('正在准备中文排版前的底图…'); self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.undo = QPushButton('撤销涂抹'); self.undo.clicked.connect(self.undo_stroke)
        self.clear = QPushButton('清空涂抹'); self.clear.clicked.connect(self.clear_strokes)
        self.preview = QPushButton('预览修复'); self.preview.clicked.connect(self.run_preview)
        self.apply = QPushButton('应用并保存'); self.apply.setObjectName('primary'); self.apply.clicked.connect(self.save_preview)
        self.cancel_task = QPushButton('取消任务'); self.cancel_task.clicked.connect(self.stop_task)
        self.close_button = QPushButton('关闭'); self.close_button.clicked.connect(self.close)
        for button in (self.undo, self.clear, self.preview, self.apply, self.cancel_task, self.close_button): footer.addWidget(button)
        layout.addLayout(footer)
        self.refresh_mask(); self.busy(False)
        QTimer.singleShot(0, self.prepare_base)

    def busy(self, running):
        for widget in (self.view, self.brush, self.paint, self.mode, self.strength, self.display, self.undo, self.clear, self.preview):
            widget.setEnabled(not running)
        self.apply.setEnabled(not running and self.preview_result is not None)
        self.cancel_task.setEnabled(running)
        self.update_painting()

    def refresh_mask(self):
        self.view.set_mask(brush_mask(self.original, self.cleanup, self.boundaries) if self.display.currentIndex() == 0 else np.zeros((self.original.height, self.original.width), np.uint8))

    def invalidate(self):
        self.preview_result = None
        self.apply.setEnabled(False)
        self.display.setCurrentIndex(0)
        self.refresh_mask()
        self.status.setText(f"已有 {len(self.cleanup['strokes'])} 笔清除范围；请预览修复后再应用。")

    def add_stroke(self, stroke):
        stroke['mode'] = self.mode.currentData()
        candidate = deepcopy(self.cleanup); candidate['strokes'].append(stroke)
        if len(candidate['strokes']) > 2000:
            self.status.setText('清除笔画过多，请先应用再继续。'); return
        if not brush_mask(self.original, {'strokes': [stroke]}, self.boundaries).any():
            self.status.setText('笔画没有覆盖可清理区域，请在边框内部涂抹。'); return
        self.history.append(deepcopy(self.cleanup['strokes']))
        self.cleanup = candidate
        self.invalidate()

    def undo_stroke(self):
        if self.history:
            self.cleanup['strokes'] = self.history.pop(); self.invalidate()

    def clear_strokes(self):
        if self.cleanup['strokes']:
            self.history.append(deepcopy(self.cleanup['strokes']))
            self.cleanup['strokes'] = []; self.invalidate()

    def strength_changed(self):
        self.cleanup['strength'] = self.strength.currentData(); self.invalidate()

    def update_painting(self):
        self.view.painting = self.paint.isChecked() and self.display.currentIndex() == 0
        self.paint.setEnabled(self.task is None and self.display.currentIndex()==0)
        self.view.setCursor(Qt.CursorShape.CrossCursor if self.view.painting else Qt.CursorShape.OpenHandCursor)

    def show_image(self):
        index = self.display.currentIndex()
        if index and self.preview_result:
            self.view.set_image(load_image(Path(self.preview_result['directory']) / ('clean.png' if index == 1 else 'translated.png')).resize(self.original.size,Image.Resampling.LANCZOS))
        else:
            self.view.set_image(self.base)
        self.update_painting(); self.refresh_mask()

    def run_task(self, function, callback):
        if self.task: return
        self.busy(True)
        task = Task(function, self); self.task = task
        task.progress.connect(lambda a, b, message: self.status.setText(message))
        task.succeeded.connect(callback)
        task.failed.connect(self.failed); task.paused.connect(self.failed)
        task.finished.connect(lambda: self.finished(task)); task.start()

    def failed(self, message):
        self.status.setText(message)

    def finished(self, task):
        self.task = None; task.deleteLater(); self.busy(False)
        if self.closing: self.close()

    def prepare_base(self):
        if self.task or self.closing: return
        if self.review_key:
            self.show_image();self.status.setText('当前版本已载入，涂抹残字后预览；只更新这一版。');return
        data = deepcopy(self.data); directory = Path(self.directory.name)
        cleanup = {'strokes': [], 'strength': self.strength.currentData()}
        source = self.library.path(self.book_id) / self.library.manifest(self.book_id)['units'][self.index]['path']
        def work(progress, cancel):
            backend = None
            try:
                if any(r.get('processing_engine') == 'mtu' for r in data.get('regions', [])):
                    from .mtu_bridge import MTUProcess
                    from types import SimpleNamespace
                    backend = MTUProcess(SimpleNamespace(root=directory), cancel); backend.gpu = self.settings.gpu
                render_page(source, data.get('regions', []), directory / 'base.png', self.settings.font_path,
                            self.settings.comic_scope, self.settings.comic_layout, data.get('manual_selections'), self.settings.comic_style,
                            settings=self.settings, backend=backend, cleanup=cleanup, clean_only=True)
                if cancel(): raise InterruptedError('准备底图已取消')
                return directory / 'base.png'
            finally:
                if backend: backend.close()
        def done(path):
            self.base = load_image(path); self.show_image()
            self.status.setText('底图已准备。涂抹残留字迹，再点击“预览修复”。')
        self.run_task(work, done)

    def run_preview(self):
        self.preview_result = None
        data, cleanup = deepcopy(self.data), deepcopy(self.cleanup)
        def done(result):
            self.preview_result = result; self.display.setCurrentIndex(1); self.show_image()
            self.status.setText('修复预览已生成。可切换“中文译图”检查；点击应用后保存清除范围及译图。')
        self.run_task(lambda progress, cancel: preview_cleanup(self.library, self.settings, self.book_id, self.index,
                      data, cleanup, Path(self.directory.name) / 'result', progress, cancel,review_key=self.review_key), done)

    def save_preview(self):
        if self.task or not self.preview_result: return
        try:
            backup = apply_cleanup(self.library, self.book_id, self.index, self.preview_result)
        except Exception as error:
            self.status.setText(str(error)); return
        self.applied = True
        self.status.setText('清理已保存，旧结果备份：' + backup)
        self.closing=True; self.directory.cleanup(); self.accept()

    def stop_task(self):
        if self.task:
            self.task.cancel(); self.status.setText('正在取消，旧译图与校对保持原样。')

    def closeEvent(self, event):
        self.closing=True
        if self.task:
            self.closing = True; self.stop_task(); event.ignore()
        else:
            self.directory.cleanup(); event.accept()

    def reject(self):
        self.close()
