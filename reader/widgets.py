from __future__ import annotations

import html
import json
import time
from pathlib import Path

from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt,Signal,QRectF,QPointF,QTimer,QEvent
from PySide6.QtGui import QImage,QPainter,QPixmap,QColor,QPen,QFont,QPalette,QTransform
from PySide6.QtWidgets import (
    QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QComboBox,QScrollArea,
    QTextBrowser,QSlider,QGraphicsView,QGraphicsScene,QGraphicsRectItem,QDialog,
    QSplitter,QListWidget,QFormLayout,QSpinBox,QPlainTextEdit,QDialogButtonBox,
    QCheckBox,QMessageBox,QLineEdit,QDoubleSpinBox,QFileDialog,QApplication,
    QTabWidget,QSizePolicy,QTableWidget,QTableWidgetItem,QHeaderView,QColorDialog,
)

from .config import Settings,get_api_key,set_api_key,write_json
from .comic_view import ContinuousComicCanvas
from .exporters import chapter_html
from .images import load_image,render_page,render_image
from .library import Library
from .pipeline import edit_path,translated_path,recognize_selection,translate_selection,apply_selection,selection_box,selection_overwrites
from .tasks import Task
from .layout_status import overflow_message,overflow_count
from .editions import edition_name,display_page,EDITIONS


class ComicCanvas(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.images=[]
        self.fit="width"
        self.setMinimumSize(100,100)

    def set_images(self,paths):
        self.images=[QImage(ImageQt(load_image(path))).copy() for path in paths]
        self.adjust_canvas()

    def adjust_canvas(self):
        if not self.images:
            return
        viewport=self.parentWidget()
        width=max(100,viewport.width() if viewport else 800)
        height=max(100,viewport.height() if viewport else 700)
        original_width=sum(image.width() for image in self.images)
        original_height=max(image.height() for image in self.images)
        factor=width/original_width
        if self.fit=="page":
            factor=min(factor,height/original_height)
        elif self.fit=="actual":
            factor=1.0
        self.factor=factor
        self.setFixedSize(max(width,int(original_width*factor)),max(height,int(original_height*factor)))
        self.update()

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.fillRect(self.rect(),self.palette().color(QPalette.ColorRole.Window))
        if not self.images:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        total_width=sum(image.width() for image in self.images)*self.factor
        x=(self.width()-total_width)/2
        for image in self.images:
            width,height=image.width()*self.factor,image.height()*self.factor
            y=max(0,(self.height()-height)/2) if self.fit=="page" else 0
            painter.drawImage(QRectF(x,y,width,height),image)
            x+=width


class ReaderWidget(QWidget):
    progress_changed=Signal(int,float)
    editor_requested=Signal()
    selection_requested=Signal()
    translation_requested=Signal(bool)
    reprocess_requested=Signal()
    upscale_requested=Signal()
    color_requested=Signal()
    pure_requested=Signal()
    back_requested=Signal()

    def __init__(self,library,settings,parent=None):
        super().__init__(parent)
        self.library=library
        self.settings=settings
        self.book_id=None
        self.manifest=None
        self.index=0
        self.loading=False
        self._active_continuous=False
        self._render_revision=0
        self._wheel_accum=0
        self._wheel_last_event=0
        self._wheel_last_turn=0
        self.auto_running=False
        self._auto_internal=False
        self._auto_elapsed=0.0
        self._auto_scroll_position=0.0
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)
        self.header=QWidget()
        header_layout=QVBoxLayout(self.header)
        header_layout.setSpacing(4)
        header=QHBoxLayout()
        controls=QHBoxLayout()
        header_layout.addLayout(header)
        header_layout.addLayout(controls)
        back=QPushButton("‹  书库")
        back.clicked.connect(self.back_requested)
        self.title=QLabel()
        self.title.setObjectName("readerTitle")
        self.version=QComboBox()
        self.version.addItems(["原版","中文版"])
        self.version.currentIndexChanged.connect(lambda:self.show_unit(self.scroll_fraction()))
        self.fit=QComboBox()
        self.fit.addItems(["适应宽度","整页显示","原始尺寸","双页显示"])
        self.fit.setCurrentIndex({"width":0,"page":1,"actual":2,"double":3}.get(settings.fit_mode,0))
        self.fit.currentIndexChanged.connect(self.change_fit)
        self.chapter=QComboBox()
        self.chapter.setMinimumWidth(180)
        self.chapter.currentIndexChanged.connect(self.jump)
        self.reading_mode=QComboBox()
        self.reading_mode.addItems(["单页阅读","整本连续预览"])
        self.reading_mode.setCurrentIndex(1 if settings.comic_read_mode=="continuous" else 0)
        self.reading_mode.currentIndexChanged.connect(self.change_reading_mode)
        self.wheel_paging=QCheckBox("滚轮翻页")
        self.wheel_paging.setChecked(settings.comic_wheel_paging)
        self.wheel_paging.setToolTip("单页模式：向下滚下一页，向上滚上一页；关闭后滚动放大的页面。")
        self.wheel_paging.toggled.connect(self.change_wheel_paging)
        edit=QPushButton("校对")
        self.editor_button=edit
        edit.clicked.connect(self.editor_requested)
        self.selection_button=QPushButton("框选翻译")
        self.selection_button.clicked.connect(self.selection_requested)
        self.upscale_button=QPushButton('超分')
        self.upscale_button.clicked.connect(self.upscale_requested)
        self.upscaled=QCheckBox('超分版')
        self.upscaled.setToolTip('切换所选原版或中文版的已有超分结果；点击“超分”设置参数并生成')
        self.upscaled.toggled.connect(lambda:self.show_unit(self.scroll_fraction()))
        self.color_button=QPushButton('上色')
        self.color_button.clicked.connect(self.color_requested)
        self.colored=QCheckBox('彩色')
        self.colored.setToolTip('显示所选版本的已有彩色结果；上色需要手动发起')
        self.colored.toggled.connect(lambda:self.show_unit(self.scroll_fraction()))
        translate=QPushButton("翻译本章 / 本页")
        translate.clicked.connect(lambda:self.translation_requested.emit(False))
        remake=QPushButton('重制本页')
        self.remake_button=remake
        remake.setToolTip('用当前流程重新识别和翻译本页；旧结果会备份，已校对文字和框选译文保留。')
        remake.clicked.connect(self.reprocess_requested)
        pure=QPushButton("无边框  F10")
        pure.clicked.connect(self.pure_requested)
        for widget in [back,self.title,self.reading_mode,self.wheel_paging,pure]:
            header.addWidget(widget)
        header.setStretch(1,1)
        for widget in [self.chapter,self.version,self.fit]:controls.addWidget(widget)
        controls.addStretch()
        controls.addWidget(edit)
        controls.addWidget(self.selection_button)
        controls.addWidget(self.upscale_button)
        controls.addWidget(self.upscaled)
        controls.addWidget(self.color_button)
        controls.addWidget(self.colored)
        controls.addWidget(translate)
        controls.addWidget(remake)
        layout.addWidget(self.header)
        self.comic=QScrollArea()
        self.comic.setFrameShape(QScrollArea.Shape.NoFrame)
        self.comic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas=ComicCanvas()
        self.comic.setWidget(self.canvas)
        self.comic.setWidgetResizable(False)
        layout.addWidget(self.comic,1)
        self.comic.viewport().installEventFilter(self)
        self.canvas.installEventFilter(self)
        self.continuous_scroll=QScrollArea()
        self.continuous_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.continuous_scroll.setWidgetResizable(False)
        self.continuous_canvas=ContinuousComicCanvas(self.continuous_scroll)
        self.continuous_scroll.setWidget(self.continuous_canvas)
        layout.addWidget(self.continuous_scroll,1)
        self.continuous_scroll.hide()
        self.novel=QTextBrowser()
        self.novel.setOpenExternalLinks(False)
        self.novel.setOpenLinks(False)
        self.novel.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.novel.document().setDocumentMargin(48)
        layout.addWidget(self.novel,1)
        self.footer=QWidget()
        footer=QHBoxLayout(self.footer)
        prev=QPushButton("上一页 / 章")
        prev.clicked.connect(lambda:self.step(-1))
        next=QPushButton("下一页 / 章")
        next.clicked.connect(lambda:self.step(1))
        self.position=QLabel()
        self.notice=QLabel()
        self.notice.setObjectName("muted")
        self.slider=QSlider(Qt.Orientation.Horizontal)
        self.slider.sliderPressed.connect(self.pause_auto_reading)
        self.slider.sliderReleased.connect(lambda:self.jump(self.slider.value()))
        self.auto_play=QPushButton("自动阅读 F8")
        self.auto_play.setCheckable(True)
        self.auto_play.clicked.connect(self.toggle_auto_reading)
        self.auto_settings=QPushButton("自动阅读设置")
        self.auto_settings.clicked.connect(self.show_auto_settings)
        for widget in [prev,self.position,self.slider,next,self.auto_play,self.auto_settings,self.notice]:
            footer.addWidget(widget)
        footer.setStretch(2,1)
        layout.addWidget(self.footer)
        self.novel.hide()
        self.comic.verticalScrollBar().valueChanged.connect(self.single_position_changed)
        self.continuous_scroll.verticalScrollBar().valueChanged.connect(self.continuous_position_changed)
        self.novel.verticalScrollBar().valueChanged.connect(self.save_progress)
        self.save_timer=QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.flush_progress)
        self.auto_timer=QTimer(self)
        self.auto_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.auto_timer.setInterval(16)
        self.auto_timer.timeout.connect(self.auto_tick)
        self._wheel_surfaces=(self.canvas,self.comic.viewport(),self.continuous_canvas,self.continuous_scroll.viewport())
        for widget in self._wheel_surfaces:
            widget.installEventFilter(self)
        for area in (self.comic,self.continuous_scroll):
            for bar in (area.verticalScrollBar(),area.horizontalScrollBar()):
                bar.installEventFilter(self)
                bar.sliderPressed.connect(self.pause_auto_reading)
                bar.valueChanged.connect(self.manual_scroll_changed)

    def clear_book(self):
        self.pause_auto_reading()
        self.flush_progress()
        self.book_id=None
        self.manifest=None
        self.save_timer.stop()
        self._active_continuous=False
        self.continuous_canvas.invalidate()
        self.continuous_canvas.pool.waitForDone()
        self.continuous_canvas.set_pages([],self.settings.fit_mode)
        self.continuous_canvas.rects=[]
        self.continuous_canvas.starts=[]
        self.continuous_canvas.resize(1,1)
        self.canvas.images=[]
        self.canvas.update()
        self.novel.clear()
        self.title.clear()
        self.position.clear()
        self.notice.clear()
        self.chapter.clear()

    def open_book(self,book_id):
        self.pause_auto_reading()
        self.flush_progress()
        self.book_id=book_id
        self.manifest=self.library.manifest(book_id)
        self._wheel_accum=0
        self._wheel_last_turn=0
        record=self.library.book(book_id)
        self.title.setText(record["title"])
        self.index=min(record["progress"],len(self.manifest["units"])-1)
        self.chapter.blockSignals(True)
        self.chapter.clear()
        for index,unit in enumerate(self.manifest["units"]):
            self.chapter.addItem(f"{index+1}. {unit['title']}")
        self.chapter.blockSignals(False)
        self.slider.setRange(0,len(self.manifest["units"])-1)
        self.fit.setVisible(self.manifest["kind"]=="comic")
        self.reading_mode.setVisible(self.manifest["kind"]=="comic")
        self.wheel_paging.setVisible(self.manifest["kind"]=="comic")
        self.auto_play.setVisible(self.manifest["kind"]=="comic")
        self.auto_settings.setVisible(self.manifest["kind"]=="comic")
        self.selection_button.setVisible(self.manifest["kind"]=="comic")
        self.upscale_button.setVisible(self.manifest['kind']=='comic')
        self.color_button.setVisible(self.manifest['kind']=='comic')
        self.colored.setVisible(self.manifest['kind']=='comic')
        self.upscaled.setVisible(self.manifest['kind']=='comic')
        self.remake_button.setVisible(self.manifest['kind']=='comic')
        self.show_unit(record["scroll"])

    def jump(self,index,automatic=False):
        if not automatic:
            self.pause_auto_reading()
        if not self.manifest or index<0 or index>=len(self.manifest["units"]):
            return
        self.flush_progress()
        self.index=index
        self.show_unit(automatic=automatic)

    def selected_edition(self):
        comic=bool(self.manifest and self.manifest['kind']=='comic')
        return edition_name(self.version.currentIndex()==1,comic and self.upscaled.isChecked())

    def select_edition(self,edition,colored=None):
        if edition not in EDITIONS:raise ValueError('阅读版本无效')
        fraction=self.scroll_fraction()
        widgets=(self.version,self.upscaled,self.colored)
        blocked=[widget.blockSignals(True) for widget in widgets]
        try:
            self.version.setCurrentIndex(1 if edition.endswith('translated') else 0)
            self.upscaled.setChecked(edition.startswith('upscaled_'))
            if colored is not None:self.colored.setChecked(colored)
        finally:
            for widget,old in zip(widgets,blocked):widget.blockSignals(old)
        self.show_unit(fraction)

    def step(self,offset,automatic=False):
        increment=2 if not self._active_continuous and self.fit.currentIndex()==3 and self.manifest and self.manifest["kind"]=="comic" else 1
        self.jump(self.index+offset*increment,automatic=automatic)

    def can_auto_advance(self):
        if not self.manifest or self.manifest["kind"]!="comic":
            return False
        if self._active_continuous:
            bar=self.continuous_scroll.verticalScrollBar()
            return bar.value()<bar.maximum()
        increment=2 if self.fit.currentIndex()==3 else 1
        return self.index+increment<len(self.manifest["units"])

    def toggle_auto_reading(self):
        if self.auto_running:
            self.pause_auto_reading()
            return
        if self.loading or not self.isVisible() or not self.can_auto_advance():
            self.auto_play.setChecked(False)
            return
        now=time.monotonic()
        self._auto_last_tick=now
        self._auto_last_save=now
        self._auto_elapsed=0.0
        self._auto_scroll_position=float(self.bar().value())
        self.auto_running=True
        self.auto_play.setChecked(True)
        self.auto_play.setText("暂停阅读 F8")
        self.update_auto_tooltip()
        self.auto_timer.start()

    def pause_auto_reading(self):
        running=self.auto_running
        self.auto_running=False
        if hasattr(self,"auto_timer"):
            self.auto_timer.stop()
        self.auto_play.setChecked(False)
        self.auto_play.setText("自动阅读 F8")
        if running:
            self.flush_progress()

    def update_auto_tooltip(self):
        rule=(f"每秒向下滚动 {self.settings.comic_auto_scroll_speed:g} 像素" if self._active_continuous
              else f"每隔 {self.settings.comic_auto_interval:g} 秒向后翻页")
        self.auto_play.setToolTip(rule+"；F8 开始或暂停。手动翻页、滚动或切换模式时暂停。")

    def show_auto_settings(self):
        self.pause_auto_reading()
        dialog=AutoReadSettingsDialog(self.settings,self.library,self)
        if dialog.exec():
            self.update_auto_tooltip()

    def auto_tick(self):
        if not self.auto_running:
            return
        now=time.monotonic()
        elapsed=max(0.0,now-self._auto_last_tick)
        self._auto_last_tick=now
        if not self.isVisible():
            self.pause_auto_reading()
            return
        if self.loading or QApplication.activeModalWidget() or QApplication.activePopupWidget():
            return
        if not self.can_auto_advance():
            self.pause_auto_reading()
            return
        if self._active_continuous:
            # Keep fractional pixels, and avoid a large jump after a stalled frame.
            self._auto_scroll_position+=max(1.0,self.settings.comic_auto_scroll_speed)*min(elapsed,.25)
            self._auto_internal=True
            try:
                self.bar().setValue(min(self.bar().maximum(),round(self._auto_scroll_position)))
            finally:
                self._auto_internal=False
        else:
            self._auto_elapsed+=elapsed
            if self._auto_elapsed>=max(1.0,self.settings.comic_auto_interval):
                self._auto_elapsed=0.0
                self.step(1,automatic=True)
        if not self.can_auto_advance():
            self.pause_auto_reading()
        elif now-self._auto_last_save>=5 and not self.loading:
            self.flush_progress()
            self._auto_last_save=now

    def manual_scroll_changed(self):
        if not self.loading and not self._auto_internal:
            self.pause_auto_reading()

    def single_position_changed(self):
        self.manual_scroll_changed()
        self.save_progress()

    def change_fit(self):
        self.settings.fit_mode=["width","page","actual","double"][self.fit.currentIndex()]
        self.settings.save(self.library.root)
        self.show_unit(self.scroll_fraction())

    def change_reading_mode(self):
        fraction=self.scroll_fraction()
        self.flush_progress()
        self.settings.comic_read_mode="continuous" if self.reading_mode.currentIndex()==1 else "single"
        self.settings.save(self.library.root)
        self.show_unit(fraction)

    def change_wheel_paging(self,checked):
        self.pause_auto_reading()
        self.settings.comic_wheel_paging=checked
        self._wheel_accum=0
        self._wheel_last_turn=0
        self.settings.save(self.library.root)

    def eventFilter(self,obj,event):
        if event.type()==QEvent.Type.Wheel:
            self.pause_auto_reading()
        if (event.type()==QEvent.Type.Wheel and self.manifest and self.manifest["kind"]=="comic"
                and not self._active_continuous and self.wheel_paging.isChecked()
                and not event.modifiers() and obj in (self.canvas,self.comic.viewport())):
            delta=event.angleDelta().y()
            threshold=120
            if not delta:
                delta=event.pixelDelta().y()
                threshold=40
            if not delta:
                return super().eventFilter(obj,event)
            now=time.monotonic()
            if now-self._wheel_last_turn<0.18:
                self._wheel_accum=0
                event.accept()
                return True
            if now-self._wheel_last_event>0.25 or delta*self._wheel_accum<0:
                self._wheel_accum=0
            self._wheel_last_event=now
            self._wheel_accum+=delta
            if abs(self._wheel_accum)>=threshold:
                self._wheel_last_turn=now
                self.step(-1 if self._wheel_accum>0 else 1)
                self._wheel_accum=0
            event.accept()
            return True
        return super().eventFilter(obj,event)

    def update_position_controls(self):
        self.chapter.blockSignals(True)
        self.chapter.setCurrentIndex(self.index)
        self.chapter.blockSignals(False)
        self.slider.setValue(self.index)
        self.position.setText(f"{self.index+1} / {len(self.manifest['units'])}")
        if self._active_continuous:
            self.notice.setText(self._continuous_missing[self.index])

    def continuous_position_changed(self):
        if self.loading or not self._active_continuous:
            return
        bar=self.continuous_scroll.verticalScrollBar()
        self.index=self.continuous_canvas.position(bar.value())[0]
        if bar.maximum()>0 and bar.value()==bar.maximum():
            self.index=len(self.manifest["units"])-1
        self.update_position_controls()
        self.save_progress()

    def show_unit(self,scroll=0,automatic=False):
        if not automatic:
            self.pause_auto_reading()
        if not self.manifest:
            return
        comic=self.manifest['kind']=='comic'
        self.editor_button.setEnabled(not comic or self.version.currentIndex()==1)
        self.editor_button.setToolTip('只修正当前中文版；其他版本保持不变' if self.version.currentIndex()==1 else '原版不提供校对，请切换到中文版')
        if comic:
            from .edition_review import ensure_page
            ensure_page(self.library,self.book_id,self.index)
        self.loading=True
        self._render_revision+=1
        revision=self._render_revision
        self._active_continuous=self.manifest["kind"]=="comic" and self.reading_mode.currentIndex()==1
        self.update_auto_tooltip()
        self.wheel_paging.setEnabled(not self._active_continuous)
        self.wheel_paging.setToolTip("连续预览使用滚轮滚动整本漫画；滚轮翻页仅在单页模式生效。" if self._active_continuous else "单页模式：向下滚下一页，向上滚上一页；关闭后滚动放大的页面。")
        self.fit.model().item(3).setEnabled(not self._active_continuous)
        if self._active_continuous and self.fit.currentIndex()==3:
            self.fit.blockSignals(True)
            self.fit.setCurrentIndex(0)
            self.fit.blockSignals(False)
            self.settings.fit_mode="width"
            self.settings.save(self.library.root)
        unit=self.manifest["units"][self.index]
        root=self.library.path(self.book_id)
        translated=self.version.currentIndex()==1
        if self.manifest["kind"]=="comic":
            self.novel.hide()
            self.comic.setVisible(not self._active_continuous)
            self.continuous_scroll.setVisible(self._active_continuous)
            indexes=list(range(len(self.manifest["units"]))) if self._active_continuous else [self.index]
            if not self._active_continuous and self.fit.currentIndex()==3 and self.index+1<len(self.manifest["units"]):
                indexes.append(self.index+1)
            if not self._active_continuous and len(indexes)==2 and self.settings.reading_direction=="rtl":
                indexes.reverse()
            paths=[]
            self._continuous_missing=[]
            for index in indexes:
                path,notice=display_page(self.library,self.book_id,index,self.selected_edition(),self.colored.isChecked())
                paths.append(path);self._continuous_missing.append(notice)
            if self._active_continuous:
                self.continuous_canvas.set_pages(paths,["width","page","actual","width"][self.fit.currentIndex()])
                self.canvas.images=[]
            else:
                self.continuous_canvas.release()
                self.canvas.fit=["width","page","actual","page"][self.fit.currentIndex()]
                self.canvas.set_images(paths)
            self.notice.setText(self._continuous_missing[self.index] if self._active_continuous else '；'.join(dict.fromkeys(n for n in self._continuous_missing if n)))
        else:
            self.comic.hide()
            self.continuous_scroll.hide()
            self.continuous_canvas.release()
            self.novel.show()
            path=edit_path(self.library,self.book_id,self.index)
            edits=json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            translations=edits.get("translations") if translated else None
            from .text_replace import suppressed_paragraphs
            content=chapter_html(unit,translations,root.as_uri()+"/",suppressed_paragraphs(edits))
            color,background=("#dbe3ee","#101923") if self.settings.theme=="dark" else ("#28313a","#f6f1e7")
            chapter_title=edits.get("title_translation") or unit["title"] if translated else unit["title"]
            if translated and edits.get('global_replacement_title',{}).get('base'):chapter_title=edits.get('title_translation','')
            self.novel.setHtml(f'<html><head><style>body{{font-family:"Microsoft YaHei";font-size:{self.settings.novel_font_size}pt;color:{color};background:{background};line-height:1.8}} p{{margin:18px 0;}} .original{{color:{"#8b99aa" if translated else color};}} img{{max-width:100%;}}</style></head><body><h2>{html.escape(chapter_title)}</h2>{content}</body></html>')
            self.novel.setStyleSheet(f"background:{background};color:{color};")
            self.notice.setText("尚未翻译，当前显示原文" if translated and translations is None else "")
        self.update_position_controls()
        QTimer.singleShot(0,lambda:self.finish_restore(revision,scroll))

    def finish_restore(self,revision,scroll):
        if revision!=self._render_revision:
            return
        if self._active_continuous:
            self.continuous_canvas.adjust_canvas()
        elif self.manifest["kind"]=="comic":
            self.canvas.adjust_canvas()
        self.restore_scroll(scroll)
        self.loading=False
        if self.auto_running:
            self._auto_last_tick=time.monotonic()
        if self._active_continuous:
            self.continuous_canvas.refresh_visible()
        self.flush_progress()

    def bar(self):
        if self.manifest and self.manifest["kind"]=="comic":
            return (self.continuous_scroll if self._active_continuous else self.comic).verticalScrollBar()
        return self.novel.verticalScrollBar()

    def scroll_fraction(self):
        bar=self.bar()
        if self._active_continuous and self.continuous_canvas.rects:
            rect=self.continuous_canvas.rects[self.index]
            return max(0.0,min(1.0,(bar.value()-rect.top())/rect.height()))
        return bar.value()/max(1,bar.maximum())

    def restore_scroll(self,fraction):
        value=self.continuous_canvas.scroll_value(self.index,fraction) if self._active_continuous else int(self.bar().maximum()*fraction)
        self.bar().setValue(value)

    def save_progress(self):
        if not self.loading:
            self.save_timer.start(300)

    def flush_progress(self):
        if self.book_id and self.manifest:
            import time
            fraction=self.scroll_fraction()
            self.library.update(self.book_id,progress=self.index,scroll=fraction,opened=time.time())
            self.progress_changed.emit(self.index,fraction)

    def set_pure(self,pure):
        self.header.setVisible(not pure)
        self.footer.setVisible(not pure)
        policy=Qt.ScrollBarPolicy.ScrollBarAlwaysOff if pure else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        for widget in [self.comic,self.continuous_scroll,self.novel]:
            widget.setVerticalScrollBarPolicy(policy)
            widget.setHorizontalScrollBarPolicy(policy)
        QTimer.singleShot(0,self.adjust_canvases)

    def adjust_canvases(self):
        if self.loading:
            return
        fraction=self.scroll_fraction()
        self.loading=True
        if self._active_continuous:
            self.continuous_canvas.adjust_canvas()
        else:
            self.canvas.adjust_canvas()
        self.restore_scroll(fraction)
        self.loading=False
        if self.auto_running:
            self._auto_scroll_position=float(self.bar().value())
            self._auto_last_tick=time.monotonic()
        self.save_progress()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        QTimer.singleShot(0,self.adjust_canvases)


class AutoReadSettingsDialog(QDialog):
    def __init__(self,settings,library,parent=None):
        super().__init__(parent)
        self.settings,self.library=settings,library
        self.setWindowTitle("自动阅读设置")
        self.setMinimumWidth(420)
        form=QFormLayout(self)
        self.interval=QDoubleSpinBox()
        self.interval.setRange(1,3600)
        self.interval.setDecimals(1)
        self.interval.setSingleStep(.5)
        self.interval.setSuffix(" 秒")
        self.interval.setValue(settings.comic_auto_interval)
        self.speed=QDoubleSpinBox()
        self.speed.setRange(1,2000)
        self.speed.setDecimals(1)
        self.speed.setSingleStep(5)
        self.speed.setSuffix(" 像素/秒")
        self.speed.setValue(settings.comic_auto_scroll_speed)
        form.addRow("翻页间隔（单页 / 双页）",self.interval)
        form.addRow("滚动速度（整本连续预览）",self.speed)
        hint=QLabel("两种模式分别保存参数。点击自动阅读或按 F8 开始 / 暂停；手动翻页、滚动和切换模式时自动暂停。")
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def save(self):
        self.settings.comic_auto_interval=self.interval.value()
        self.settings.comic_auto_scroll_speed=self.speed.value()
        self.settings.save(self.library.root)
        self.accept()


class BoxView(QGraphicsView):
    box_added=Signal(object)
    box_selected=Signal(int)
    layout_box_selected=Signal(object)
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.adding=False
        self.layout_selecting=False
        self.start=None
        self.temporary=None
        self.rectangles=[]
        self.zoomed=False

    def load(self,path):
        self.load_image(load_image(path))

    def load_image(self,image,coordinate_size=None):
        self.scene().clear()
        self.rectangles=[]
        self.start=None;self.temporary=None
        pixmap=QPixmap.fromImage(QImage(ImageQt(image)).copy())
        self.image_item=self.scene().addPixmap(pixmap)
        if coordinate_size:
            self.image_item.setTransform(QTransform.fromScale(coordinate_size[0]/image.width,coordinate_size[1]/image.height))
        self.zoomed=False
        self.scene().setSceneRect(self.image_item.sceneBoundingRect())
        self.fitInView(self.image_item,Qt.AspectRatioMode.KeepAspectRatio)

    def pixmap(self):
        return self.image_item.pixmap() if hasattr(self,'image_item') else QPixmap()

    def clear(self):
        self.cancel_drawing();self.scene().clear();self.rectangles=[]
        if hasattr(self,'image_item'):del self.image_item

    def cancel_drawing(self):
        if self.temporary is not None:self.scene().removeItem(self.temporary)
        self.start=None;self.temporary=None;self.layout_selecting=False
        self.unsetCursor()

    def begin_layout_selection(self):
        self.cancel_drawing();self.adding=False;self.layout_selecting=True
        self.setCursor(Qt.CursorShape.CrossCursor)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,"image_item") and not self.zoomed:
            self.fitInView(self.image_item,Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self,event):
        factor=1.15 if event.angleDelta().y()>0 else 1/1.15
        size=self.transform().m11()*factor
        if .02<size<12:
            self.zoomed=True
            self.scale(factor,factor)
        event.accept()

    def boxes(self,regions,selected=-1,scope="dialogue"):
        from .comic_regions import region_enabled
        for item in self.rectangles:
            self.scene().removeItem(item)
        self.rectangles=[]
        for index,region in enumerate(regions):
            pending=bool(region.get('render_overflow'))
            color="#ff5b6b" if pending else ("#f8c86a" if index==selected else ("#40d9bd" if region_enabled(region,scope) else "#8893a5"))
            pen=QPen(QColor(color),3 if pending and index==selected else 2)
            if pending:pen.setCosmetic(True)
            box=region.get('layout_box') or region['box']
            item=self.scene().addRect(QRectF(*box),pen)
            if pending:item.setToolTip(overflow_message(region))
            item.setData(0,index)
            item.setZValue(2)
            label=self.scene().addSimpleText(str(index+1))
            label.setBrush(QColor("#ff5b6b" if pending else "#f8c86a"))
            label.setPos(box[0],box[1]-18)
            label.setZValue(3)
            self.rectangles.extend([item,label])

    def mousePressEvent(self,event):
        position=self.mapToScene(event.position().toPoint())
        if (self.adding or self.layout_selecting) and event.button()==Qt.MouseButton.LeftButton and hasattr(self,'image_item'):
            self.start=position
            self.temporary=self.scene().addRect(QRectF(position,position),QPen(QColor('#65c7ff' if self.layout_selecting else '#f8c86a'),2))
            self.temporary.setZValue(10)
            return
        item=self.itemAt(event.position().toPoint())
        if item and isinstance(item,QGraphicsRectItem) and item.data(0) is not None:
            self.box_selected.emit(item.data(0))
        super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        if self.start is not None:
            self.temporary.setRect(QRectF(self.start,self.mapToScene(event.position().toPoint())).normalized())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):
        if self.start is not None:
            rectangle=QRectF(self.start,self.mapToScene(event.position().toPoint())).normalized().intersected(self.image_item.sceneBoundingRect())
            selecting=self.layout_selecting
            self.cancel_drawing()
            self.adding=False
            if rectangle.width()>5 and rectangle.height()>5:
                box=[int(rectangle.x()),int(rectangle.y()),int(rectangle.width()),int(rectangle.height())]
                (self.layout_box_selected if selecting else self.box_added).emit(box)
            return
        super().mouseReleaseEvent(event)


class SelectionTranslationDialog(QDialog):
    def __init__(self,library,settings,book_id,index,parent=None,*,colored=False,edition=None,review_key=None):
        super().__init__(parent)
        self.library,self.settings,self.book_id,self.index=library,settings,book_id,index
        self.manifest=library.manifest(book_id)
        self.image=load_image(library.path(book_id)/self.manifest["units"][index]["path"])
        self.color_image=None
        self.review_key=review_key
        display_image=None
        display_notice=''
        if edition is not None:
            path,display_notice=display_page(library,book_id,index,edition,colored)
            display_image=load_image(path)
            self.color_image=display_image.resize(self.image.size)
        elif colored:
            from .colorization import available_color
            path=next((p for edition in ('translated','upscaled_translated','original','upscaled_original') if (p:=available_color(library,book_id,index,edition))),None)
            if path:self.color_image=load_image(path).resize(self.image.size)
        self.selection=None
        self.current=-1
        self.task=None
        self._next_translate=False
        self._closing=False
        self.applied=False
        self.setWindowTitle(f"框选识别与翻译 · 第 {index+1} 页")
        self.resize(1220,850)
        layout=QVBoxLayout(self)
        self.hint=QLabel("在原图上按住鼠标左键拖出矩形，只识别框内日文。滚轮缩放；选区确定后可拖动查看，点击“重新框选”更换范围。")
        self.hint.setWordWrap(True)
        if display_notice:self.hint.setText(self.hint.text()+' '+display_notice)
        layout.addWidget(self.hint)
        split=QSplitter()
        layout.addWidget(split,1)
        self.tabs=QTabWidget()
        self.view=BoxView()
        self.view.load(library.path(book_id)/self.manifest["units"][index]["path"])
        if display_image:self.view.load_image(display_image,self.image.size)
        elif self.color_image:self.view.load_image(self.color_image)
        self.view.adding=True
        self.view.box_added.connect(self.set_box)
        self.view.box_selected.connect(lambda index:self.list.setCurrentRow(index))
        self.view.layout_box_selected.connect(self.set_layout_area)
        self.outline=self.view.scene().addRect(QRectF(),QPen(QColor("#f8c86a"),3))
        self.outline.setZValue(5)
        self.tabs.addTab(self.view,"原图框选")
        self.preview=BoxView()
        self.preview.setMinimumSize(240,300)
        self.preview.box_selected.connect(lambda index:self.list.setCurrentRow(index))
        self.preview.layout_box_selected.connect(self.set_layout_area)
        self.tabs.addTab(self.preview,"译文预览与框选")
        split.addWidget(self.tabs)
        panel=QWidget()
        controls=QVBoxLayout(panel)
        self.list=QListWidget()
        self.list.setMaximumHeight(115)
        self.list.currentRowChanged.connect(self.select_region)
        controls.addWidget(self.list)
        self.source=QPlainTextEdit()
        self.translation=QPlainTextEdit()
        self.source.setPlaceholderText("识别出的日文可在此修正")
        self.translation.setPlaceholderText("中文译文可在此修改，也可手动填写")
        controls.addWidget(QLabel("日文 / OCR 结果"))
        controls.addWidget(self.source,1)
        controls.addWidget(QLabel("中文译文"))
        controls.addWidget(self.translation,1)
        self.direction=QComboBox()
        self.direction.addItems(["跟随原文方向","竖排（右列先读）","横排"])
        self.font_size=QSpinBox()
        self.font_size.setRange(0,128)
        self.font_size.setSpecialValueText("自动适配")
        self.weight=QComboBox()
        self.font_size.setToolTip("字号为上限；文字必须完整留在气泡与边框内，必要时自动缩小。")
        self.weight.addItems(["跟随原文粗细","常规","加粗"])
        self.mode=QComboBox()
        self.mode.addItems(["修复文字背景","白色填充","背景色填充"])
        options=QFormLayout()
        options.addRow("译文排版",self.direction)
        options.addRow("字号",self.font_size)
        options.addRow("文字粗细",self.weight)
        options.addRow("原字处理",self.mode)
        controls.addLayout(options)
        area_buttons=QHBoxLayout()
        self.layout_area_button=QPushButton('框选译文范围')
        self.layout_area_button.clicked.connect(self.begin_layout_area)
        self.reset_layout_button=QPushButton('恢复自动范围')
        self.reset_layout_button.clicked.connect(self.reset_layout_area)
        area_buttons.addWidget(self.layout_area_button);area_buttons.addWidget(self.reset_layout_button)
        controls.addLayout(area_buttons)
        self.remove=QPushButton("移除当前识别块")
        self.remove.clicked.connect(self.remove_region)
        controls.addWidget(self.remove)
        split.addWidget(panel)
        split.setSizes([790,430])
        footer=QHBoxLayout()
        self.reselect=QPushButton("重新框选")
        self.reselect.clicked.connect(self.begin_selection)
        self.ocr_button=QPushButton("只识别")
        self.ocr_button.clicked.connect(lambda:self.recognize(False))
        self.translate_button=QPushButton("识别并翻译")
        self.translate_button.clicked.connect(lambda:self.recognize(True))
        self.preview_button=QPushButton("预览排版")
        self.preview_button.clicked.connect(self.show_preview)
        self.apply_button=QPushButton("应用并保存")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self.apply)
        self.cancel_task_button=QPushButton("取消任务")
        self.cancel_task_button.clicked.connect(self.cancel_task)
        self.close_button=QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        for button in (self.reselect,self.ocr_button,self.translate_button,self.preview_button,self.apply_button,self.cancel_task_button,self.close_button):
            footer.addWidget(button)
        layout.addLayout(footer)
        self.busy(False)

    def busy(self,running):
        for widget in (self.view,self.preview,self.list,self.source,self.translation,self.direction,self.font_size,self.weight,self.mode,self.layout_area_button,self.reset_layout_button,
                       self.remove,self.reselect,self.ocr_button,self.translate_button,self.preview_button,self.apply_button):
            widget.setEnabled(not running)
        self.cancel_task_button.setEnabled(running)

    def begin_selection(self):
        self.view.cancel_drawing();self.preview.cancel_drawing()
        self.view.adding=True
        self.tabs.setCurrentIndex(0)
        self.hint.setText("按住鼠标左键拖出新的矩形；原选区会在完成新框选后替换。")

    def set_box(self,box):
        if self.task:
            return
        try:
            box=selection_box(box,self.image.size)
        except ValueError as error:
            self.hint.setText(str(error))
            return
        self.selection={"box":box,"regions":[]}
        self.current=-1
        self.outline.setRect(QRectF(*box))
        self.view.adding=False
        path=edit_path(self.library,self.book_id,self.index)
        if path.exists():
            from copy import deepcopy
            data=json.loads(path.read_text(encoding="utf-8"))
            existing=next((p for p in reversed(data.get("manual_selections",[])) if p["box"]==box),None)
            if existing:
                self.selection=deepcopy(existing)
        self.populate()
        self.preview.clear()
        self.hint.setText("选区已确定。点击只识别，或识别并翻译；识别结果可修改后再次翻译。")

    def commit(self):
        if self.selection and 0<=self.current<len(self.selection["regions"]):
            region=self.selection["regions"][self.current]
            region.update(text=self.source.toPlainText(),translation=self.translation.toPlainText(),
                          layout=["auto","vertical","horizontal"][self.direction.currentIndex()],
                          font_size=self.font_size.value(),mode=["inpaint","white","solid"][self.mode.currentIndex()],
                          weight=["auto","normal","bold"][self.weight.currentIndex()])

    def populate(self):
        self.current=-1
        self.list.blockSignals(True)
        self.list.clear()
        for i,r in enumerate(self.selection["regions"] if self.selection else []):
            self.list.addItem(f"{i+1}. "+('[译文放不下]' if r.get('render_overflow') else '')+r.get('text','')[:30])
            if r.get('render_overflow'):self.list.item(i).setForeground(QColor('#ff5b6b'))
        self.list.blockSignals(False)
        self.source.clear()
        self.translation.clear()
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.view.boxes([])

    def select_region(self,index):
        self.view.cancel_drawing();self.preview.cancel_drawing()
        self.commit()
        self.current=index
        if not self.selection or index<0:
            return
        region=self.selection["regions"][index]
        self.source.setPlainText(region.get("text",""))
        self.translation.setPlainText(region.get("translation",""))
        self.direction.setCurrentIndex({"auto":0,"vertical":1,"horizontal":2}.get(region.get("layout"),0))
        self.font_size.setValue(region.get("font_size",0))
        self.weight.setCurrentIndex({"auto":0,"normal":1,"bold":2}.get(region.get("weight"),0))
        self.mode.setCurrentIndex({"inpaint":0,"white":1,"solid":2}.get(region.get("mode"),0))
        regions=self.display_regions()
        self.view.boxes(regions,index,"all")
        if hasattr(self.preview,'image_item'):self.preview.boxes(regions,index,'all')

    def display_regions(self):
        from copy import deepcopy
        regions=deepcopy(self.selection['regions']) if self.selection else []
        for region in regions:
            for key in ('box','layout_box'):
                if key in region:
                    region[key][0]+=self.selection['box'][0];region[key][1]+=self.selection['box'][1]
        return regions

    def begin_layout_area(self):
        if not self.selection or self.current<0:
            self.hint.setText('请先识别并选中一个文字块，再框选译文范围。');return
        self.commit()
        self.view.cancel_drawing();self.preview.cancel_drawing()
        active=self.preview if self.tabs.currentIndex()==1 and hasattr(self.preview,'image_item') else self.view
        if active is self.view:self.tabs.setCurrentIndex(0)
        active.begin_layout_selection()
        self.hint.setText('在图上按住左键框选更大的译文范围；原识别文字会保留，完成后点击“预览排版”或“应用并保存”。')

    def set_layout_area(self,box):
        if self.task or not self.selection or self.current<0:return
        from .layout_area import enlarged_box,grow_selection
        self.commit();selected=self.current;region=self.selection['regions'][selected]
        ox,oy=self.selection['box'][:2];x,y,w,h=region['box']
        try:area=enlarged_box([x+ox,y+oy,w,h],box,self.image.size)
        except ValueError as error:self.hint.setText(str(error));return
        self.selection.setdefault('_replace_box',list(self.selection['box']))
        outer=grow_selection(self.selection,area,self.image.size)
        region['layout_box']=[area[0]-outer[0],area[1]-outer[1],*area[2:]]
        region['style_reviewed']=True
        self.outline.setRect(QRectF(*outer))
        self.populate();self.list.setCurrentRow(selected)
        self.hint.setText('译文范围已扩大，识别结果与中文保留；预览或保存时会重新适配字号并检查边框。')

    def reset_layout_area(self):
        if not self.selection or self.current<0:return
        self.commit();selected=self.current
        self.selection['regions'][selected].pop('layout_box',None)
        self.populate();self.list.setCurrentRow(selected)
        self.hint.setText('已恢复自动范围，请重新预览或应用并保存。')

    def remove_region(self):
        if self.selection and self.current>=0:
            self.selection["regions"].pop(self.current)
            self.populate()

    def run_task(self,function,callback):
        if self.task:
            return
        self.busy(True)
        task=Task(function,self)
        self.task=task
        task.progress.connect(lambda a,b,message:self.hint.setText(message))
        task.succeeded.connect(callback)
        task.failed.connect(self.failed)
        task.paused.connect(self.failed)
        task.finished.connect(lambda:self.finished(task))
        task.start()

    def failed(self,message):
        self._next_translate=False
        self.hint.setText(message)

    def finished(self,task):
        self.task=None
        self.busy(False)
        task.deleteLater()
        if self._closing:
            self.close()
        elif self._next_translate:
            self._next_translate=False
            self.translate()

    def recognize(self,and_translate):
        if not self.selection:
            self.hint.setText("请先在原图中拖出选区。")
            return
        self.commit()
        if and_translate and self.selection["regions"]:
            self.translate()
            return
        box=list(self.selection["box"])
        def done(result):
            self.selection=result
            self.populate()
            self._next_translate=and_translate and bool(result["regions"]) and not self._closing and not self.task.cancelled.is_set()
            self.hint.setText("识别完成，可修改日文后翻译。" if result["regions"] else "选区内未识别到文字，请调整范围后重试。")
        self.run_task(lambda progress,cancel:recognize_selection(self.library,self.book_id,self.index,box,self.settings,progress,cancel),done)

    def translate(self):
        from copy import deepcopy
        self.commit()
        selection=deepcopy(self.selection)
        def done(result):
            self.selection=result
            self.populate()
            self.hint.setText("翻译完成。可修改译文、预览排版，再应用并保存。")
        self.run_task(lambda progress,cancel:translate_selection(self.library,self.book_id,self.index,selection,self.settings,progress,cancel),done)

    def show_preview(self):
        try:
            self.commit()
            if not self.selection or not self.selection["regions"]:
                raise ValueError("请先识别并翻译选区")
            from .text_replace import apply_global_replacements,has_translated_text
            apply_global_replacements(self.selection,self.settings)
            if any(not has_translated_text(r) for r in self.selection["regions"]):
                raise ValueError("请先翻译所有文字块，或移除无需翻译的文字块")
            x,y,w,h=self.selection["box"]
            from .comic_regions import PageBoundaries
            path=edit_path(self.library,self.book_id,self.index)
            data=json.loads(path.read_text('utf-8')) if path.exists() else {}
            cleanup=dict(data.get('residual_cleanup',{}));cleanup.setdefault('strength',self.settings.comic_cleanup_strength)
            image=render_image(self.image.crop((x,y,x+w,y+h)),self.selection["regions"],self.settings.font_path,"all",self.settings.comic_layout,self.settings.comic_style,
                               boundaries=PageBoundaries(self.image),boundary_offset=(x,y),cleanup=cleanup,gpu=self.settings.gpu)
            composite=(self.color_image or self.image).copy();composite.paste(image,(x,y))
            self.preview.load_image(composite)
            self.tabs.setCurrentIndex(1)
            selected=self.current;self.populate();self.list.setCurrentRow(selected)
            pending=overflow_count(self.selection)
            self.hint.setText(f'{pending} 个译文放不下，已标红；完整中文已保留，可修改后重新预览。' if pending else '排版预览完成，译文均在安全范围内。')
        except Exception as error:
            self.hint.setText(str(error))

    def apply(self):
        try:
            self.commit()
            if not self.selection:
                raise ValueError("请先框选区域")
            if selection_overwrites(self.library,self.book_id,self.index,self.selection["box"],review_key=self.review_key):
                answer=QMessageBox.question(self,"覆盖选区译文","选区内已有译文，是否用当前结果覆盖？框外内容保持不变。",
                                           QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)
                if answer!=QMessageBox.StandardButton.Yes:
                    return
            apply_selection(self.library,self.book_id,self.index,self.selection,self.settings,review_key=self.review_key)
            self.applied=True
            selected=self.current;self.populate();self.list.setCurrentRow(selected)
            pending=overflow_count(self.selection)
            self.hint.setText(f'已保存；{pending} 个译文放不下，已标红待校对。修改后重新应用即可恢复正常框。' if pending else "已保存到中文版，框外内容保持不变。可继续框选其他区域，或关闭返回阅读。")
        except Exception as error:
            self.hint.setText(str(error))

    def cancel_task(self):
        self._next_translate=False
        if self.task:
            self.task.cancel()
            self.hint.setText("正在取消；已完成识别结果保留，当前请求结束后停止。")

    def closeEvent(self,event):
        if self.task:
            self._closing=True
            self.cancel_task()
            event.ignore()
        else:
            event.accept()

    def reject(self):
        self.close()


class TextReplaceDialog(QDialog):
    def __init__(self,library,settings,book_id,index,region_index=-1,parent=None,*,review_key=None):
        super().__init__(parent)
        self.library,self.settings,self.book_id,self.index,self.region_index=library,settings,book_id,index,region_index
        self.result_info=None;self.plan=None;self.task=None
        self.review_key=review_key
        self.setWindowTitle('查找与替换 · 漫画文本');self.resize(950,640)
        layout=QVBoxLayout(self);form=QFormLayout()
        self.find=QLineEdit();self.find.setPlaceholderText('要查找的文字（按字面匹配）')
        self.replacement=QLineEdit();self.replacement.setPlaceholderText('替换为；留空表示删除匹配文字')
        self.scope=QComboBox()
        for label,value in [('当前文字框','region'),('当前页','page'),('整本漫画','book')]:self.scope.addItem(label,value)
        self.scope.setCurrentIndex(1)
        if review_key:
            from .edition_review import LABELS
            self.setWindowTitle('查找与替换 · '+LABELS[review_key])
            self.scope.setItemText(2,'整本漫画的当前版本（已生成页）')
        self.field=QComboBox();self.field.addItem('中文译文','translation');self.field.addItem('OCR 原文','text')
        self.case_sensitive=QCheckBox('区分大小写');self.case_sensitive.setChecked(True)
        for label,widget in [('查找',self.find),('替换为',self.replacement),('范围',self.scope),('修改内容',self.field),('',self.case_sensitive)]:form.addRow(label,widget)
        layout.addLayout(form)
        note=QLabel('先预览命中结果，再执行替换。只重绘受影响页面，保留字体、字号与范围设置；旧校对和译图自动备份。OCR 原文替换不修改原图，不自动重新翻译，不调用翻译接口。')
        note.setWordWrap(True);layout.addWidget(note)
        self.status=QLabel('请输入查找文字，点击“预览命中”。');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['页码','位置','次数','替换前','替换后'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table,1)
        buttons=QHBoxLayout();self.preview_button=QPushButton('预览命中');self.apply_button=QPushButton('替换并生成译图')
        self.apply_button.setObjectName('primary');self.apply_button.setEnabled(False)
        self.close_button=QPushButton('关闭');self.cancel_button=QPushButton('取消此次替换');self.cancel_button.setVisible(False)
        for widget in (self.preview_button,self.apply_button,self.cancel_button,self.close_button):buttons.addWidget(widget)
        layout.addLayout(buttons)
        self.preview_button.clicked.connect(self.preview);self.apply_button.clicked.connect(self.apply)
        self.cancel_button.clicked.connect(lambda:self.task.cancel() if self.task else None)
        self.close_button.clicked.connect(self.close)
        for widget in (self.find,self.replacement):widget.textChanged.connect(self.invalidate)
        for widget in (self.scope,self.field):widget.currentIndexChanged.connect(self.invalidate)
        self.case_sensitive.toggled.connect(self.invalidate)

    def invalidate(self,*_):
        self.plan=None;self.apply_button.setEnabled(False);self.table.setRowCount(0)
        self.status.setText('条件已改变，请重新预览命中。')

    def preview(self):
        from .text_replace import preview_replace
        try:
            self.plan=preview_replace(self.library,self.book_id,self.index,self.find.text(),self.replacement.text(),
                                      self.scope.currentData(),self.field.currentData(),self.region_index,self.case_sensitive.isChecked(),review_key=self.review_key)
        except (OSError,ValueError) as error:self.status.setText(str(error));self.plan=None;return
        rows=self.plan['rows'];self.table.setRowCount(len(rows))
        for n,row in enumerate(rows):
            for col,key in enumerate(('page','frame','count','before','after')):
                item=QTableWidgetItem(str(row[key]));item.setToolTip(str(row[key]));self.table.setItem(n,col,item)
        self.status.setText(f"命中 {self.plan['matches']} 处，涉及 {len(self.plan['pages'])} 页；执行后会重新生成这些页面的译图。")
        self.apply_button.setEnabled(bool(self.plan['matches']))

    def apply(self):
        if not self.plan or self.task:return
        from .text_replace import apply_replace
        self.task=Task(lambda progress,cancel:apply_replace(self.library,self.settings,self.plan,progress,cancel),self)
        for widget in (self.find,self.replacement,self.scope,self.field,self.case_sensitive,self.preview_button,self.apply_button,self.close_button):widget.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.task.progress.connect(lambda a,b,message:self.status.setText(f'{a}/{b} · {message}'))
        self.task.succeeded.connect(self.completed)
        self.task.failed.connect(self.status.setText);self.task.paused.connect(self.status.setText)
        self.task.finished.connect(self.finished_task);self.task.start()

    def completed(self,result):
        self.result_info=result
        self.status.setText(f"已替换 {result['matches']} 处，生成 {len(result['indexes'])} 页译图。备份：{result['backup']}")
        self.plan=None

    def finished_task(self):
        self.task.deleteLater();self.task=None;self.cancel_button.setVisible(False)
        for widget in (self.find,self.replacement,self.scope,self.field,self.case_sensitive,self.preview_button,self.close_button):widget.setEnabled(True)
        self.apply_button.setEnabled(False)

    def reject(self):
        if not self.task:super().reject()

    def closeEvent(self,event):
        if self.task:event.ignore();self.status.setText('请等待替换完成，或点击“取消此次替换”。')
        else:super().closeEvent(event)


class EditorDialog(QDialog):
    render_requested=Signal(object)
    def __init__(self,library,settings,book_id,index,parent=None,*,colored=False,edition=None,review_key=None):
        super().__init__(parent)
        self.library,self.settings,self.book_id,self.index=library,settings,book_id,index
        self.manifest=library.manifest(book_id)
        self.unit=self.manifest["units"][index]
        self.review_key=review_key
        self._guard_unsaved=review_key is not None
        if self.manifest['kind']=='comic' and review_key is not None:
            from .edition_review import ensure_page,data_path,load_data,LABELS,valid_image
            ensure_page(library,book_id,index)
            if not valid_image(library,book_id,index,review_key):raise ValueError('当前中文版尚未生成，请先生成对应版本')
            self.path=data_path(library,book_id,index,review_key)
            self.data=load_data(library,book_id,index,review_key)
        else:
            self.path=edit_path(library,book_id,index)
            self.data=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.current=-1
        self.setWindowTitle("校对与排版 · "+self.unit["title"])
        if review_key is not None:self.setWindowTitle('校对与排版 · '+LABELS[review_key]+' · '+self.unit['title'])
        self.resize(1250,820)
        layout=QVBoxLayout(self)
        self.hint=QLabel("绿色框翻译，灰色框保留，红色框表示译文缩小后仍放不下：完整中文已保存，暂时保留原文，其余文字继续翻译。修改译文、字号或字体后点击“保存并生成译图”；确认能完整放入后自动取消红框。" if self.manifest["kind"]=="comic" else "逐段修改中文译文，保存后可立即阅读或导出。")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        splitter=QSplitter()
        if review_key is not None:
            label=QLabel('当前校对：'+LABELS[review_key]+'。保存只更新这一版；图片选择仅供预览参考。')
            label.setWordWrap(True);layout.addWidget(label)
        layout.addWidget(splitter,1)
        if self.manifest["kind"]=="comic":
            self.view=BoxView()
            self.view.load(library.path(book_id)/self.unit["path"])
            self.data.setdefault("regions",[])
            from .comic_regions import annotate_regions,annotate_boundaries,PageBoundaries
            original=load_image(library.path(book_id)/self.unit['path'])
            self.original_image=original
            self.page_boundaries=PageBoundaries(original)
            annotate_regions(original, self.data["regions"])
            annotate_boundaries(original,self.data['regions'],self.page_boundaries)
            self.view.box_added.connect(self.add_box)
            self.view.box_selected.connect(lambda index:self.list.setCurrentRow(index))
            self.view.layout_box_selected.connect(self.set_layout_area)
            splitter.addWidget(self.view)
        else:
            self.data.setdefault("translations",[""]*len(self.unit["paragraphs"]))
            self.original=QTextBrowser()
            self.original.setPlainText("\n\n".join(self.unit["paragraphs"]))
            splitter.addWidget(self.original)
        panel=QWidget()
        panel_layout=QVBoxLayout(panel)
        self.list=QListWidget()
        self.list.setMaximumHeight(180)
        panel_layout.addWidget(self.list)
        self.source=QPlainTextEdit()
        self.source.setPlaceholderText("原文 / OCR 结果")
        self.translation=QPlainTextEdit()
        self.translation.setPlaceholderText("中文译文")
        panel_layout.addWidget(QLabel("原文"))
        panel_layout.addWidget(self.source,1)
        panel_layout.addWidget(QLabel("译文"))
        panel_layout.addWidget(self.translation,1)
        if self.manifest["kind"]=="comic":
            form=QFormLayout()
            self.font_face=QComboBox()
            self.font_face.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
            self.font_face.setMinimumWidth(80)
            from .embedded_runtime import FONT_CHOICES
            for label,value in [('跟随全局设置',''),*FONT_CHOICES]:
                self.font_face.addItem(label,value)
            font_row=QWidget()
            font_layout=QHBoxLayout(font_row);font_layout.setContentsMargins(0,0,0,0)
            font_layout.addWidget(self.font_face,1)
            browse=QPushButton('导入字体…');browse.clicked.connect(self.browse_region_font)
            font_layout.addWidget(browse)
            form.addRow('译文字体',font_row)
            self.font_size=QSpinBox()
            self.font_size.setRange(0,128)
            self.font_size.setSpecialValueText("自动适配")
            self.font_size.setSuffix(' px')
            self.font_size.setKeyboardTracking(False)
            self.font_size.editingFinished.connect(lambda:self.font_size.setValue(max(8,self.font_size.value())) if self.font_size.value() else None)
            self.font_size_slider=QSlider(Qt.Orientation.Horizontal)
            self.font_size_slider.setRange(0,128)
            self.font_size_slider.setToolTip('拖动调节字号；最左端为自动适配，手动最小 8 px。')
            self.font_size_slider.valueChanged.connect(lambda value:self.font_size.setValue(0 if value==0 else max(8,value)))
            self.font_size.valueChanged.connect(self.font_size_slider.setValue)
            size_row=QWidget();size_layout=QHBoxLayout(size_row);size_layout.setContentsMargins(0,0,0,0)
            size_layout.addWidget(self.font_size,1);size_layout.addWidget(self.font_size_slider,1)
            form.addRow("译文字号",size_row)
            self.font_size.setToolTip("可输入 8–128 像素；0 为自动适配。字号为上限，放不下会自动缩小，确保不越出气泡与边框。")
            self.actual_size=QLabel()
            self.actual_size.setWordWrap(True)
            form.addRow('上次生成字号',self.actual_size)
            self.coordinates=[]
            for label in ["横坐标","纵坐标","宽度","高度"]:
                spin=QSpinBox()
                spin.setRange(0 if len(self.coordinates)<2 else 6,100000)
                form.addRow(label,spin)
                self.coordinates.append(spin)
            self.weight=QComboBox()
            self.weight.addItems(["跟随原文粗细","常规","加粗"])
            form.addRow("文字粗细",self.weight)
            self.text_layout=QComboBox()
            self.text_layout.addItems(["跟随设置 / 原文方向","竖排（右列先读）","横排"])
            form.addRow("译文排版",self.text_layout)
            self.selection=QComboBox()
            self.selection.addItems(["自动（按气泡识别）","翻译此框","保留此框原文"])
            form.addRow("翻译范围",self.selection)
            self.balloon_status=QLabel()
            self.balloon_status.setWordWrap(True)
            form.addRow("识别结果",self.balloon_status)
            self.mode=QComboBox()
            self.mode.addItems(["修复文字背景","白色填充（白气泡）","背景色填充（纯色区域）","保留原文"])
            form.addRow("原字处理",self.mode)
            form_panel=QWidget();form_panel.setLayout(form)
            form_scroll=QScrollArea();form_scroll.setWidgetResizable(True);form_scroll.setWidget(form_panel)
            form_scroll.setMinimumHeight(200)
            panel_layout.addWidget(form_scroll,2)
            buttons=QHBoxLayout()
            add=QPushButton("新增文字框")
            add.clicked.connect(self.begin_add_box)
            remove=QPushButton("移除文字框")
            remove.clicked.connect(self.remove_box)
            buttons.addWidget(add)
            buttons.addWidget(remove)
            panel_layout.addLayout(buttons)
            area_buttons=QHBoxLayout()
            self.layout_area_button=QPushButton('框选译文范围')
            self.layout_area_button.clicked.connect(self.begin_layout_area)
            self.reset_layout_button=QPushButton('恢复自动范围')
            self.reset_layout_button.clicked.connect(self.reset_layout_area)
            area_buttons.addWidget(self.layout_area_button);area_buttons.addWidget(self.reset_layout_button)
            panel_layout.addLayout(area_buttons)
            self.image_version=QComboBox()
            self.image_version.addItems(['显示原图','显示已生成译图'])
            self.image_version.addItems(['显示彩色原版','显示彩色中文版'])
            self.image_version.addItems(['显示原版超分','显示中文版超分','显示彩色原版超分','显示彩色中文版超分'])
            self.image_version.currentIndexChanged.connect(self.change_image_version)
            panel_layout.addWidget(self.image_version)
        else:
            self.source.setReadOnly(True)
            panel_layout.addWidget(QLabel("章节标题译文"))
            self.chapter_translation=QLineEdit(self.data.get("title_translation",""))
            self.chapter_translation.setPlaceholderText(self.unit["title"])
            panel_layout.addWidget(self.chapter_translation)
        splitter.addWidget(panel)
        splitter.setSizes([800,400])
        self.list.currentRowChanged.connect(self.select)
        self.populate()
        if edition is not None and self.manifest['kind']=='comic':
            choice={('original',False):0,('translated',False):1,('original',True):2,('translated',True):3,
                    ('upscaled_original',False):4,('upscaled_translated',False):5,('upscaled_original',True):6,('upscaled_translated',True):7}[edition,colored]
            self.image_version.setCurrentIndex(choice)
            if choice==0:self.change_image_version(0)
        elif colored and self.manifest['kind']=='comic':
            from .colorization import available_color
            translated=available_color(library,book_id,index,'translated') or available_color(library,book_id,index,'upscaled_translated')
            self.image_version.setCurrentIndex(3 if translated else 2)
        footer=QHBoxLayout()
        close=QPushButton("关闭")
        close.clicked.connect(self.close)
        if self.manifest['kind']=='comic':
            self.cleanup_button=QPushButton('清除残字…')
            self.cleanup_button.clicked.connect(self.open_cleanup)
            footer.addWidget(self.cleanup_button)
            replace_text=QPushButton('查找与替换…')
            replace_text.clicked.connect(self.open_text_replace)
            footer.addWidget(replace_text)
        save=QPushButton("保存校对")
        save.clicked.connect(self.save)
        render=QPushButton("保存并生成译图" if self.manifest["kind"]=="comic" else "保存并返回阅读")
        render.setObjectName("primary")
        render.clicked.connect(self.save_and_render)
        footer.addStretch()
        for button in [close,save,render]:
            footer.addWidget(button)
        layout.addLayout(footer)

    def populate(self):
        from .edition_review import draft_signature
        if not hasattr(self,'_saved_signature'):self._saved_signature=draft_signature(self.data)
        self.list.blockSignals(True)
        self.list.clear()
        if self.manifest["kind"]=="comic":
            self.region_rows=[(region,self.data['regions'],(0,0),'') for region in self.data['regions']]
            for p,patch in enumerate(self.data.get('manual_selections',[])):
                self.region_rows.extend((region,patch['regions'],tuple(patch['box'][:2]),f'框选{p+1}·{n+1} ')
                                        for n,region in enumerate(patch.get('regions',[])))
            for index,row in enumerate(self.region_rows):
                self.list.addItem('');self.update_region_item(index)
        else:
            for index,text in enumerate(self.unit["paragraphs"]):
                self.list.addItem(f"{index+1}. {text[:35]}")
        self.list.blockSignals(False)
        self.current=-1
        if self.list.count():
            pending=next((i for i,row in enumerate(self.region_rows) if row[0].get('render_overflow')),0) if self.manifest['kind']=='comic' else 0
            self.list.setCurrentRow(pending)

    def display_regions(self):
        result=[]
        for region,_,offset,_ in self.region_rows:
            item=dict(region);x,y,w,h=region['box'];item['box']=[x+offset[0],y+offset[1],w,h]
            if region.get('layout_box'):
                a,b,c,d=region['layout_box'];item['layout_box']=[a+offset[0],b+offset[1],c,d]
            result.append(item)
        return result

    def update_region_item(self,index):
        from .comic_regions import region_enabled
        region,_,_,prefix=self.region_rows[index]
        state="翻译" if region_enabled(region,self.settings.comic_scope) else "保留"
        label='译文放不下·待校对' if region.get('render_overflow') else ('重复识别待确认' if region.get('render_warning') else ('边界待确认' if region.get('boundary_warning') else ('识别待校对' if region.get('ocr_warning') and not region.get('ocr_reviewed') else state)))
        item=self.list.item(index)
        item.setText(f"{index+1}. {prefix}[{label}] {region.get('text','')[:35] or '新文字框'}")
        item.setForeground(QColor('#ff5b6b') if region.get('render_overflow') else self.palette().color(QPalette.ColorRole.Text))
        item.setToolTip(overflow_message(region))

    def commit(self):
        if self.current<0:
            return
        if self.manifest["kind"]=="comic":
            region,_,offset,_=self.region_rows[self.current]
            if region.get('text','')!=self.source.toPlainText() or region.get('translation','')!=self.translation.toPlainText():
                region['ocr_reviewed']=True
                region.pop('ocr_warning',None)
            font_path=self.font_face.currentData() or ''
            size=self.font_size.value()
            if size:size=max(8,size)
            weight=["auto","normal","bold"][self.weight.currentIndex()]
            text_layout=["auto","vertical","horizontal"][self.text_layout.currentIndex()]
            if (region.get('font_path','')!=font_path or region.get('font_size',0)!=size or
                region.get('weight','auto')!=weight or region.get('layout','auto')!=text_layout):
                region['style_reviewed']=True
            region.update(text=self.source.toPlainText(),translation=self.translation.toPlainText(),
                          box=[self.coordinates[0].value()-offset[0],self.coordinates[1].value()-offset[1],self.coordinates[2].value(),self.coordinates[3].value()],font_size=size,font_path=font_path,
                          mode=["inpaint","white","solid","keep"][self.mode.currentIndex()],
                          layout=["auto","vertical","horizontal"][self.text_layout.currentIndex()],
                          selection=["auto","include","exclude"][self.selection.currentIndex()])
            region["weight"]=["auto","normal","bold"][self.weight.currentIndex()]
            from .comic_regions import region_enabled,annotate_boundaries
            if offset==(0,0):annotate_boundaries(self.original_image,[region],self.page_boundaries)
            self.update_region_item(self.current)
        else:
            self.data["translations"][self.current]=self.translation.toPlainText()

    def select(self,index):
        if self.manifest['kind']=='comic':self.view.cancel_drawing()
        self.commit()
        self.current=index
        if index<0:
            return
        if self.manifest["kind"]=="comic":
            region,_,offset,_=self.region_rows[index]
            self.source.setPlainText(region.get("text",""))
            self.translation.setPlainText(region.get("translation",""))
            x,y,w,h=region['box']
            for spin,value in zip(self.coordinates,[x+offset[0],y+offset[1],w,h]):
                spin.setValue(int(value))
            self.font_size.setValue(region.get("font_size",0))
            font_path=region.get('font_path','')
            face_index=self.font_face.findData(font_path)
            if face_index<0:
                self.font_face.addItem(Path(font_path.removeprefix('user-font:')).name,font_path)
                face_index=self.font_face.count()-1
            self.font_face.setCurrentIndex(face_index)
            actual=region.get('render_font_size')
            self.actual_size.setText('缩小至 8 px 后仍放不下，待校对' if region.get('render_overflow') else (f'{actual} px（重新生成后更新）' if actual is not None else '尚未生成'))
            self.weight.setCurrentIndex({"auto":0,"normal":1,"bold":2}.get(region.get("weight"),0))
            self.mode.setCurrentIndex({"inpaint":0,"white":1,"solid":2,"keep":3}.get(region.get("mode"),0))
            self.text_layout.setCurrentIndex({"auto":0,"vertical":1,"horizontal":2}.get(region.get("layout"),0))
            self.selection.setCurrentIndex({"auto":0,"include":1,"exclude":2}.get(region.get("selection"),0))
            self.balloon_status.setText(overflow_message(region) or region.get('render_warning') or region.get('boundary_warning') or region.get('ocr_warning') or ("封闭白气泡" if region.get("is_dialogue") else "未确认气泡，默认保留"))
            self.view.boxes(self.display_regions(),index,self.settings.comic_scope)
        else:
            self.source.setPlainText(self.unit["paragraphs"][index])
            self.translation.setPlainText(self.data["translations"][index])

    def begin_add_box(self):
        self.view.cancel_drawing();self.view.adding=True

    def begin_layout_area(self):
        if self.current<0:
            self.hint.setText('请先选中一个文字框。');return
        self.commit();self.view.begin_layout_selection()
        self.hint.setText('按住鼠标左键框选更大的译文范围；原文识别与中文保留，完成后点击“保存并生成译图”。')

    def set_layout_area(self,box):
        if self.current<0:return
        from .layout_area import enlarged_box,grow_selection
        self.commit();selected=self.current
        region,container,offset,_=self.region_rows[selected];x,y,w,h=region['box']
        try:area=enlarged_box([x+offset[0],y+offset[1],w,h],box,self.original_image.size)
        except ValueError as error:self.hint.setText(str(error));return
        if container is not self.data['regions']:
            patch=next(p for p in self.data['manual_selections'] if p['regions'] is container)
            offset=grow_selection(patch,area,self.original_image.size)[:2]
        region['layout_box']=[area[0]-offset[0],area[1]-offset[1],*area[2:]]
        region['style_reviewed']=True
        self.current=-1;self.populate();self.list.setCurrentRow(selected)
        self.hint.setText('译文范围已扩大，保存并生成译图后重新适配字号；气泡和分镜边界仍受保护。')

    def reset_layout_area(self):
        if self.current<0:return
        self.commit();self.region_rows[self.current][0].pop('layout_box',None)
        self.view.boxes(self.display_regions(),self.current,self.settings.comic_scope)
        self.hint.setText('已恢复自动范围，点击“保存并生成译图”应用。')

    def change_image_version(self,index):
        self.commit()
        edition,colored=[('original',False),('translated',False),('original',True),('translated',True),
                         ('upscaled_original',False),('upscaled_translated',False),('upscaled_original',True),('upscaled_translated',True)][index]
        path,notice=display_page(self.library,self.book_id,self.index,edition,colored)
        self.view.cancel_drawing()
        image=load_image(path)
        self.view.load_image(image,self.original_image.size)
        self.view.boxes(self.display_regions(),self.current,self.settings.comic_scope)
        if notice:self.hint.setText(notice+'；可处理后重新打开校对。')

    def browse_region_font(self):
        path,_=QFileDialog.getOpenFileName(self,'导入中文字体','','字体文件 (*.ttf *.ttc *.otf)')
        if not path:return
        from .embedded_runtime import import_font
        try:value=import_font(path)
        except (OSError,ValueError) as error:
            QMessageBox.warning(self,'字体导入失败',str(error));return
        index=self.font_face.findData(value)
        if index<0:
            self.font_face.addItem(Path(path).name,value);index=self.font_face.count()-1
        self.font_face.setCurrentIndex(index)

    def open_text_replace(self):
        self.save()
        selected=self.current
        dialog=TextReplaceDialog(self.library,self.settings,self.book_id,self.index,selected,self,review_key=self.review_key)
        dialog.exec()
        if dialog.result_info:
            self.current=-1
            self.data=json.loads(self.path.read_text('utf-8'))
            self.populate()
            self.list.setCurrentRow(min(selected,self.list.count()-1))
            from .edition_review import draft_signature
            self._saved_signature=draft_signature(self.data)
            self.hint.setText(f"已替换 {dialog.result_info['matches']} 处，重新生成 {len(dialog.result_info['indexes'])} 页译图；旧结果已备份。")

    def open_cleanup(self):
        self.save()
        from .cleanup_widgets import CleanupDialog
        selected=self.current
        dialog=CleanupDialog(self.library,self.settings,self.book_id,self.index,self.data,self,review_key=self.review_key)
        dialog.exec()
        if dialog.applied:
            self.current=-1
            self.data=json.loads(self.path.read_text('utf-8'))
            self.populate()
            self.list.setCurrentRow(min(selected,self.list.count()-1))
            from .edition_review import draft_signature
            self._saved_signature=draft_signature(self.data)
            self.hint.setText('残字清理已保存，重新排版时会继续应用；旧校对和译图已备份。')

    def add_box(self,box):
        self.commit()
        self.data["regions"].append({"box":box,"text":"","translation":"","font_size":0,"mode":"white",
                                     "selection":"include","layout":"auto"})
        self.populate()
        self.list.setCurrentRow(len(self.data['regions'])-1)

    def remove_box(self):
        if self.current>=0:
            region,container,_,_=self.region_rows[self.current]
            container.pop(next(n for n,value in enumerate(container) if value is region))
            self.current=-1
            self.populate()
            self.view.boxes(self.display_regions(),scope=self.settings.comic_scope)

    def save(self):
        self.commit()
        if self.manifest["kind"]=="novel":
            self.data["title_translation"]=self.chapter_translation.text()
        else:
            from .comic_regions import annotate_regions
            annotate_regions(load_image(self.library.path(self.book_id)/self.unit["path"]), self.data["regions"])
            self.view.boxes(self.display_regions(),self.current,self.settings.comic_scope)
        write_json(self.path,self.data)
        from .edition_review import draft_signature
        self._saved_signature=draft_signature(self.data)
        self.hint.setText("当前版本的校对数据已保存；其他版本保持不变。" if self.review_key else "校对数据已保存。")

    def save_and_render(self):
        self.save()
        if self.manifest['kind']=='novel':
            from .text_replace import apply_global_replacements
            apply_global_replacements(self.data,self.settings)
            write_json(self.path,self.data)
        if self.manifest["kind"]=="comic":
            self.render_requested.emit(self.data)
        self.accept()

    def closeEvent(self,event):
        if self._guard_unsaved:
            self.commit()
            from .edition_review import draft_signature
            if draft_signature(self.data)!=self._saved_signature:
                answer=QMessageBox.question(self,'尚未保存校对','当前版本还有未保存的修改。是否保存校对？',
                    QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)
                if answer==QMessageBox.StandardButton.Cancel:event.ignore();return
                if answer==QMessageBox.StandardButton.Save:self.save()
        else:self.save()
        event.accept()
        super().reject()

    def reject(self):
        self.close()


class SettingsDialog(QDialog):
    def __init__(self,settings,library,parent=None):
        super().__init__(parent)
        self.settings,self.library=settings,library
        self.setWindowTitle("设置 · 翻译与阅读")
        self.resize(640,800)
        outer=QVBoxLayout(self)
        scroll=QScrollArea()
        scroll.setWidgetResizable(True)
        content=QWidget()
        layout=QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll,1)
        explanation=QLabel("填写兼容 Chat Completions 的接口与模型名称。漫画默认只上传识别出的文字；API 密钥可仅用于本次运行，或保存到 Windows 凭据管理器。")
        from .appearance import AppearanceOptions
        self.appearance_options=AppearanceOptions(settings)
        layout.addWidget(self.appearance_options)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        form=QFormLayout()
        self.url=QLineEdit(settings.api_url)
        self.model=QLineEdit(settings.model)
        self.model.setPlaceholderText("填写服务商提供的模型名称")
        self.comic_engine=QComboBox();self.comic_engine.addItems(['内置高质量（漫画检测、可选 OCR、LaMa、智能排版）','原有漫画流程（固定 Manga OCR）'])
        self.comic_engine.setCurrentIndex(0 if settings.comic_engine=='mtu' else 1)
        self.comic_ocr=QComboBox()
        self.comic_ocr.addItem('48px · 漫画文字识别','48px')
        self.comic_ocr.addItem('mocr · Manga OCR 日文识别','mocr')
        self.comic_ocr.setCurrentIndex(1 if settings.comic_ocr=='mocr' else 0)
        self.comic_ocr.setToolTip('用于高质量流程的整页与手动框选识别。已有页面请点“重制本页”重新识别；人工校对继续保留。')
        self.comic_ocr.setEnabled(settings.comic_engine=='mtu')
        self.comic_engine.currentIndexChanged.connect(lambda index:self.comic_ocr.setEnabled(index==0))
        self.mtu_path=QLineEdit('阅译内置：48px / mocr / LaMa / MangaLens')
        self.mtu_path.setReadOnly(True)
        self.key=QLineEdit(get_api_key())
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.persist=QCheckBox("将密钥保存到系统凭据管理器")
        self.target=QComboBox()
        self.target.addItems(["简体中文","繁體中文"])
        self.target.setCurrentText(settings.target_language)
        self.json_mode=QCheckBox("接口支持 JSON 模式时启用")
        self.json_mode.setChecked(settings.json_mode)
        self.gpu=QCheckBox("可用时使用 GPU 进行日语 OCR")
        self.gpu.setChecked(settings.gpu)
        self.comic_scope=QComboBox()
        self.comic_scope.addItems(["只翻译对话气泡（其他原文保留）","翻译所有文字框"])
        self.comic_scope.setCurrentIndex(0 if settings.comic_scope=="dialogue" else 1)
        self.comic_layout=QComboBox()
        self.comic_layout.addItems(["跟随原文方向","全部竖排（右列先读）","全部横排"])
        self.comic_layout.setCurrentIndex({"auto":0,"vertical":1,"horizontal":2}.get(settings.comic_layout,0))
        self.comic_style=QComboBox()
        self.comic_style.addItems(["参考译本（原文字号、粗细与留白）","原有排版（尽量填满文字框）"])
        self.comic_style.setCurrentIndex(0 if settings.comic_style=="reference" else 1)
        self.cleanup_strength=QComboBox()
        self.cleanup_strength.addItem('标准（保留原有擦除范围）','standard')
        self.cleanup_strength.addItem('加强（在安全边界内扩大擦除范围）','enhanced')
        self.cleanup_strength.setCurrentIndex(max(0,self.cleanup_strength.findData(settings.comic_cleanup_strength)))
        self.requests=QSpinBox()
        self.requests.setRange(1,100000)
        self.requests.setValue(settings.max_requests)
        self.batch=QSpinBox()
        self.batch.setRange(100,10000)
        self.batch.setValue(settings.batch_characters)
        self.input_price=QDoubleSpinBox()
        self.output_price=QDoubleSpinBox()
        self.budget=QDoubleSpinBox()
        for widget,value in [(self.input_price,settings.input_price),(self.output_price,settings.output_price),(self.budget,settings.budget)]:
            widget.setRange(0,100000)
            widget.setDecimals(4)
            widget.setValue(value)
        self.font_size=QSpinBox()
        self.font_size.setRange(10,40)
        self.font_size.setValue(settings.novel_font_size)
        self.direction=QComboBox()
        self.direction.addItems(["从右到左（日漫）","从左到右"])
        self.direction.setCurrentIndex(0 if settings.reading_direction=="rtl" else 1)
        self.theme=QComboBox()
        self.theme.addItems(["深色","纸张"])
        self.theme.setCurrentIndex(0 if settings.theme=="dark" else 1)
        self.font_path=QLineEdit(settings.font_path)
        font_row=QWidget()
        font_layout=QHBoxLayout(font_row)
        font_layout.setContentsMargins(0,0,0,0)
        choose=QPushButton("选择字体")
        choose.clicked.connect(self.choose_font)
        font_layout.addWidget(self.font_path,1)
        self.font_library=QComboBox()
        self.font_library.addItem('选择内置字体…',None)
        from .embedded_runtime import FONT_CHOICES
        for label,value in FONT_CHOICES:self.font_library.addItem(label,value)
        self.font_library.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
        self.font_library.setMinimumWidth(80)
        self.font_library.currentIndexChanged.connect(lambda _:self.font_path.setText(self.font_library.currentData()) if self.font_library.currentData() else None)
        font_layout.addWidget(self.font_library)
        font_layout.addWidget(choose)
        recommended=QPushButton("内置中文字体")
        recommended.setToolTip("随阅译携带，无需另行安装字体。builtin 表示内置字体。")
        recommended.clicked.connect(lambda:self.font_path.setText("builtin"))
        font_layout.addWidget(recommended)
        for label,widget in [("接口地址",self.url),("模型",self.model),("API 密钥",self.key),("",self.persist),
                             ('漫画处理流程',self.comic_engine),('漫画 OCR 模型',self.comic_ocr),('内置漫画引擎',self.mtu_path),
                             ("目标语言",self.target),("",self.json_mode),("",self.gpu),
                             ("漫画翻译范围",self.comic_scope),("漫画译文排版",self.comic_layout),("单次任务请求上限",self.requests),
                             ("漫画排版风格",self.comic_style),
                             ("原字自动清理",self.cleanup_strength),
                             ("每批原文字符数",self.batch),("输入单价 / 百万 tokens",self.input_price),
                             ("输出单价 / 百万 tokens",self.output_price),("单次任务费用阈值（0 为关闭）",self.budget),
                             ("小说字号",self.font_size),("漫画阅读方向",self.direction),("小说主题",self.theme),("漫画中文字体",font_row)]:
            form.addRow(label,widget)
        layout.addLayout(form)
        self.glossary=QPlainTextEdit(settings.glossary)
        self.glossary.setPlaceholderText("人物与术语表，例如：\n田中 → 田中\n魔法陣 → 魔法阵")
        layout.addWidget(QLabel("翻译术语表"))
        layout.addWidget(self.glossary,1)
        layout.addWidget(QLabel('全局文本替换'))
        from .upscale_widgets import UpscaleOptions
        layout.addWidget(QLabel('漫画超分默认选项（点击阅读工具栏“超分”开始处理）'))
        self.upscale_options=UpscaleOptions(settings)
        layout.addWidget(self.upscale_options)
        from .color_widgets import ColorOptions
        layout.addWidget(QLabel('漫画上色默认选项（仅手动点击“上色”时运行）'))
        self.color_options=ColorOptions(settings)
        layout.addWidget(self.color_options)
        self.global_replace_enabled=QCheckBox('启用全局文本替换（只处理中文译文）')
        self.global_replace_enabled.setChecked(settings.text_replace_enabled)
        layout.addWidget(self.global_replace_enabled)
        self.global_replace_case=QCheckBox('区分大小写')
        self.global_replace_case.setChecked(settings.text_replace_case_sensitive)
        layout.addWidget(self.global_replace_case)
        self.global_rules_table=QTableWidget(0,3)
        self.global_rules_table.setHorizontalHeaderLabels(['启用','查找文字','替换为（可留空）'])
        self.global_rules_table.setColumnWidth(0,55)
        self.global_rules_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        self.global_rules_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        self.global_rules_table.setMinimumHeight(150);self.global_rules_table.setMaximumHeight(210)
        for rule in settings.text_replace_rules:self.add_global_rule(rule)
        if not self.global_rules_table.rowCount():self.add_global_rule()
        layout.addWidget(self.global_rules_table)
        rule_buttons=QHBoxLayout();add_rule=QPushButton('添加规则');remove_rule=QPushButton('删除选中规则')
        add_rule.clicked.connect(lambda:self.add_global_rule());remove_rule.clicked.connect(self.remove_global_rule)
        rule_buttons.addWidget(add_rule);rule_buttons.addWidget(remove_rule);rule_buttons.addStretch();layout.addLayout(rule_buttons)
        rule_note=QLabel('保存后，在后续翻译、框选翻译和重新生成时应用。按字面匹配，较长查找文字优先，替换结果不会在同一轮再次匹配。保留替换前文本；修改或关闭规则后，重新生成可更新结果。替换为留空表示删除匹配文字。')
        rule_note.setWordWrap(True);layout.addWidget(rule_note)
        preview_row=QHBoxLayout();self.global_preview_input=QLineEdit();self.global_preview_input.setPlaceholderText('输入示例中文，测试规则效果')
        preview_button=QPushButton('测试替换');preview_button.clicked.connect(self.preview_global_rules)
        preview_row.addWidget(self.global_preview_input,1);preview_row.addWidget(preview_button);layout.addLayout(preview_row)
        self.global_preview_output=QLabel();self.global_preview_output.setWordWrap(True);layout.addWidget(self.global_preview_output)
        note=QLabel("识别、修复、字体和超分模型已内置。漫画与小说使用已配置的云端接口翻译。云端翻译和下载资源需要联网。费用按填写单价估算，阈值不等同于服务商账单硬限额。")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def choose_font(self):
        path,_=QFileDialog.getOpenFileName(self,"选择中文字体","C:/Windows/Fonts","字体 (*.ttf *.ttc *.otf)")
        if path:
            self.font_path.setText(path)

    def add_global_rule(self,rule=None):
        rule=rule or {};row=self.global_rules_table.rowCount();self.global_rules_table.insertRow(row)
        enabled=QTableWidgetItem();enabled.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable|Qt.ItemFlag.ItemIsUserCheckable)
        enabled.setCheckState(Qt.CheckState.Checked if rule.get('enabled',True) else Qt.CheckState.Unchecked)
        self.global_rules_table.setItem(row,0,enabled)
        self.global_rules_table.setItem(row,1,QTableWidgetItem(rule.get('find','')))
        self.global_rules_table.setItem(row,2,QTableWidgetItem(rule.get('replace','')))

    def remove_global_rule(self):
        rows={i.row() for i in self.global_rules_table.selectedIndexes()}
        for row in sorted(rows,reverse=True):self.global_rules_table.removeRow(row)

    def read_global_rules(self):
        rules=[]
        for row in range(self.global_rules_table.rowCount()):
            find=self.global_rules_table.item(row,1);replacement=self.global_rules_table.item(row,2)
            query=find.text() if find else '';value=replacement.text() if replacement else ''
            if query or value:rules.append({'find':query,'replace':value,'enabled':self.global_rules_table.item(row,0).checkState()==Qt.CheckState.Checked})
        return rules

    def preview_global_rules(self):
        from dataclasses import replace
        from .text_replace import replace_global_text,global_rules
        settings=replace(self.settings,text_replace_enabled=self.global_replace_enabled.isChecked(),
                         text_replace_case_sensitive=self.global_replace_case.isChecked(),text_replace_rules=self.read_global_rules())
        try:
            global_rules(settings)
            self.global_preview_output.setText('替换结果：'+replace_global_text(self.global_preview_input.text(),settings))
        except ValueError as error:self.global_preview_output.setText(str(error))

    def save(self):
        from dataclasses import replace
        if self.color_options.task:
            QMessageBox.information(self,'正在检测','请等待上色连接检测完成');return
        try:
            from .color_sizes import output_size
            output_size(replace(self.settings,**self.color_options.values()))
        except ValueError as error:
            QMessageBox.warning(self,'上色尺寸无效',str(error));return
        if self.budget.value()>0 and self.input_price.value()==0 and self.output_price.value()==0:
            QMessageBox.information(self,"填写单价","启用费用阈值前，请填写翻译服务的输入与输出单价。")
            return
        try:background_color=self.appearance_options.value()
        except ValueError as error:
            QMessageBox.warning(self,'背景颜色无效',str(error));return
        from dataclasses import replace
        from .text_replace import global_rules
        rules=self.read_global_rules()
        try:global_rules(replace(self.settings,text_replace_rules=rules,text_replace_case_sensitive=self.global_replace_case.isChecked()))
        except ValueError as error:
            QMessageBox.warning(self,'替换规则无效',str(error));return
        try:
            set_api_key(self.key.text().strip(),self.persist.isChecked())
            self.color_options.save_key()
        except Exception as error:
            QMessageBox.warning(self,"无法保存密钥",str(error)+"\n可取消保存到系统凭据，仅用于本次运行。")
            return
        values={"background_color":background_color,"api_url":self.url.text().strip(),"model":self.model.text().strip(),"target_language":self.target.currentText(),
                'comic_engine':'mtu' if self.comic_engine.currentIndex()==0 else 'classic',
                'comic_ocr':self.comic_ocr.currentData(),
                'mtu_root':'',
                "json_mode":self.json_mode.isChecked(),"gpu":self.gpu.isChecked(),"max_requests":self.requests.value(),
                "comic_scope":"dialogue" if self.comic_scope.currentIndex()==0 else "all",
                "comic_layout":["auto","vertical","horizontal"][self.comic_layout.currentIndex()],
                "comic_style":["reference","classic"][self.comic_style.currentIndex()],
                "comic_cleanup_strength":self.cleanup_strength.currentData(),
                "batch_characters":self.batch.value(),"input_price":self.input_price.value(),"output_price":self.output_price.value(),
                "budget":self.budget.value(),"novel_font_size":self.font_size.value(),"reading_direction":"rtl" if self.direction.currentIndex()==0 else "ltr",
                "theme":"dark" if self.theme.currentIndex()==0 else "paper","font_path":self.font_path.text(),"glossary":self.glossary.toPlainText(),
                'text_replace_enabled':self.global_replace_enabled.isChecked(),'text_replace_case_sensitive':self.global_replace_case.isChecked(),
                'text_replace_rules':rules}
        for key,value in values.items():
            setattr(self.settings,key,value)
        for key,value in self.upscale_options.values().items():setattr(self.settings,key,value)
        for key,value in self.color_options.values().items():setattr(self.settings,key,value)
        self.settings.save(self.library.root)
        from .appearance import apply_appearance
        apply_appearance(QApplication.instance(),self.settings)
        self.accept()

    def reject(self):
        if self.color_options.task:
            QMessageBox.information(self,'正在检测','请等待上色连接检测完成');return
        super().reject()
