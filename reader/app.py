from __future__ import annotations

import html
import json
import os
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt,QEvent,QTimer,QByteArray,QSize,QUrl,QRect,QLockFile
from PySide6.QtGui import QAction,QDesktopServices,QIcon,QKeySequence,QShortcut,QPixmap
from PySide6.QtWidgets import (
    QApplication,QMainWindow,QWidget,QHBoxLayout,QVBoxLayout,QLabel,QPushButton,
    QStackedWidget,QListWidget,QListWidgetItem,QLineEdit,QComboBox,QFileDialog,
    QMessageBox,QMenu,QDialog,QFormLayout,QDialogButtonBox,QTableWidget,
    QTableWidgetItem,QHeaderView,QAbstractItemView,QProgressBar,QCheckBox,
    QInputDialog,QSpinBox,QTabWidget,
)

from .config import Settings,get_api_key,data_root,write_json
from .exporters import export_book,backup_library
from .images import render_page
from .importers import import_book
from .library import Library,safe_name
from .pipeline import translate_book,translated_path,edit_path
from .tasks import Task
from .activity import format_timestamp
from .widgets import ReaderWidget,EditorDialog,SettingsDialog,SelectionTranslationDialog

from .appearance import STYLE,apply_appearance



def heading(text,muted=False):
    label=QLabel(text)
    label.setObjectName("muted" if muted else "heading")
    label.setWordWrap(True)
    return label


def cover_thumbnail(pixmap):
    """Reserve the same cover area for portrait, landscape and small images."""
    from PySide6.QtGui import QPainter
    canvas=QPixmap(160,215)
    canvas.fill(Qt.GlobalColor.transparent)
    scaled=pixmap.scaled(canvas.size(),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
    painter=QPainter(canvas)
    painter.drawPixmap((canvas.width()-scaled.width())//2,(canvas.height()-scaled.height())//2,scaled)
    painter.end()
    return canvas


class MainWindow(QMainWindow):
    def __init__(self,library=None,settings=None):
        super().__init__()
        self.library=library or Library()
        self.settings=settings or Settings.load(self.library.root)
        apply_appearance(QApplication.instance(),self.settings)
        self.active_task=None
        self.pure=False
        self.space_held=False
        self.pure_drag=None
        self.closing=False
        self.setWindowTitle("阅译 · 漫画与小说")
        self.resize(1450,920)
        self.setMinimumSize(700,500)
        central=QWidget()
        self.root_layout=QHBoxLayout(central)
        self.root_layout.setContentsMargins(0,0,0,0)
        self.root_layout.setSpacing(0)
        self.setCentralWidget(central)
        self.sidebar=QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(195)
        side=QVBoxLayout(self.sidebar)
        side.setContentsMargins(14,30,14,20)
        brand=QLabel("読  阅译")
        brand.setObjectName("brand")
        side.addWidget(brand)
        side.addWidget(heading("你的本地阅读空间",True))
        side.addSpacing(30)
        self.navigation=[]
        for label,index in [("我的书库",0),("任务与用量",2)]:
            button=QPushButton(label)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(lambda checked,i=index:self.navigate(i))
            self.navigation.append((button,index))
            side.addWidget(button)
        side.addStretch()
        settings_button=QPushButton("翻译与阅读设置")
        settings_button.clicked.connect(self.show_settings)
        side.addWidget(settings_button)
        side.addWidget(heading("日语 → 中文\n原版与译版分别保存",True))
        self.root_layout.addWidget(self.sidebar)
        self.stack=QStackedWidget()
        self.root_layout.addWidget(self.stack,1)
        self.library_page=self.build_library()
        self.stack.addWidget(self.library_page)
        self.reader=ReaderWidget(self.library,self.settings)
        self.reader.back_requested.connect(lambda:self.navigate(0))
        self.reader.pure_requested.connect(self.toggle_pure)
        self.reader.translation_requested.connect(self.translate)
        self.reader.reprocess_requested.connect(lambda:self.translate(False,reprocess=True))
        self.reader.editor_requested.connect(self.edit_current)
        self.reader.selection_requested.connect(self.select_translation)
        self.reader.upscale_requested.connect(self.upscale_current)
        self.reader.color_requested.connect(self.color_current)
        self.reader.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.reader.customContextMenuRequested.connect(self.reader_menu)
        self.stack.addWidget(self.reader)
        self.tasks_page=self.build_tasks()
        self.stack.addWidget(self.tasks_page)
        self.status=self.statusBar()
        self.task_label=QLabel("就绪")
        self.progress=QProgressBar()
        self.progress.setFixedWidth(200)
        self.progress.setRange(0,100)
        self.progress.hide()
        self.cancel_button=QPushButton("暂停任务")
        self.cancel_button.clicked.connect(self.cancel_task)
        self.cancel_button.hide()
        self.status.addWidget(self.task_label,1)
        self.status.addPermanentWidget(self.progress)
        self.status.addPermanentWidget(self.cancel_button)
        shortcuts={"F8":self.toggle_auto_reader,"F10":self.toggle_pure,"F11":self.toggle_fullscreen,"Escape":self.escape,
                   "Ctrl+O":self.import_files,"Ctrl+Shift+O":self.import_folder,
                   "Ctrl+,":self.show_settings,"Ctrl+T":lambda:self.translate(False),
                   "Ctrl+E":self.export_current,"Left":lambda:self.direction_step(-1),
                   "Right":lambda:self.direction_step(1),"PageDown":lambda:self.reader_step(1),
                   "PageUp":lambda:self.reader_step(-1)}
        self.shortcuts=[]
        for key,callback in shortcuts.items():
            shortcut=QShortcut(QKeySequence(key),self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        self.setAcceptDrops(True)
        QApplication.instance().installEventFilter(self)
        self.refresh_library()
        self.navigate(0)
        if self.settings.geometry:
            self.restoreGeometry(QByteArray.fromBase64(self.settings.geometry.encode()))
        screens=QApplication.screens()
        if not any(screen.availableGeometry().intersects(self.frameGeometry()) for screen in screens):
            self.move(screens[0].availableGeometry().topLeft())
        records=self.library.list_books()
        if self.settings.frameless and records and records[0]["opened"]:
            self.open_book(records[0]["id"])
            QTimer.singleShot(0,self.toggle_pure)
        if self.settings.topmost:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,True)

    def build_library(self):
        page=QWidget()
        layout=QVBoxLayout(page)
        layout.setContentsMargins(28,28,28,20)
        top=QHBoxLayout()
        text=QVBoxLayout()
        text.addWidget(heading("我的书库"))
        self.count=heading("漫画与小说，在这里接着读。",True)
        text.addWidget(self.count)
        top.addLayout(text,1)
        import_button=QPushButton("＋  导入文件")
        import_button.setObjectName("primary")
        import_button.clicked.connect(self.import_files)
        folder_button=QPushButton("导入漫画文件夹")
        folder_button.clicked.connect(self.import_folder)
        top.addWidget(folder_button)
        top.addWidget(import_button)
        layout.addLayout(top)
        layout.addSpacing(20)
        filters=QHBoxLayout()
        self.search=QLineEdit()
        self.search.setPlaceholderText("搜索作品、作者或标签")
        self.search.textChanged.connect(self.refresh_library)
        self.kind_filter=QComboBox()
        self.kind_filter.addItems(["全部作品","漫画","小说"])
        self.kind_filter.currentIndexChanged.connect(self.refresh_library)
        self.favorite_filter=QCheckBox("只看收藏")
        self.favorite_filter.stateChanged.connect(self.refresh_library)
        filters.addWidget(self.search,1)
        filters.addWidget(self.kind_filter)
        filters.addWidget(self.favorite_filter)
        layout.addLayout(filters)
        self.book_list=QListWidget()
        self.book_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.book_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.book_list.setMovement(QListWidget.Movement.Static)
        self.book_list.setIconSize(QSize(160,215))
        self.book_list.setGridSize(QSize(220,335))
        self.book_list.setSpacing(3)
        self.book_list.setWordWrap(True)
        self.book_list.itemDoubleClicked.connect(lambda item:self.open_book(item.data(Qt.ItemDataRole.UserRole)))
        self.book_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.book_list.customContextMenuRequested.connect(self.book_menu)
        layout.addWidget(self.book_list,1)
        self.empty=heading("书库还是空的\n\n导入图片文件夹、CBZ、TXT 或 EPUB 开始阅读。\n也可以将文件直接拖入窗口。",True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty,1)
        footer=QHBoxLayout()
        open_button=QPushButton("打开阅读")
        open_button.clicked.connect(self.open_selected)
        translate=QPushButton("翻译作品")
        translate.clicked.connect(lambda:self.translate(True))
        export=QPushButton("导出译本")
        export.clicked.connect(self.export_current)
        backup=QPushButton("备份书库")
        backup.clicked.connect(self.backup)
        delete=QPushButton("删除作品")
        delete.clicked.connect(lambda:self.delete_book(self.selected_book()))
        for button in [open_button,translate,export,backup,delete]:
            footer.addWidget(button)
        footer.addStretch()
        layout.addLayout(footer)
        return page

    def build_tasks(self):
        page=QWidget()
        layout=QVBoxLayout(page)
        layout.setContentsMargins(28,28,28,20)
        layout.addWidget(heading("任务与用量"))
        self.usage_label=heading("",True)
        layout.addWidget(self.usage_label)
        layout.addWidget(heading('时间精确到秒（UTC+8）；旧任务未记录的开始时间显示“未记录”。按住 Ctrl 或 Shift 可选择多条记录。',True))
        tabs=QTabWidget();self.activity_tabs=tabs
        tasks_page=QWidget();tasks_layout=QVBoxLayout(tasks_page)
        self.jobs_table=QTableWidget(0,8)
        self.jobs_table.setHorizontalHeaderLabels(["作品 / 任务","类型","状态","进度","开始时间","结束时间","更新时间","说明"])
        self.jobs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.jobs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.jobs_table.horizontalHeader().setSectionResizeMode(7,QHeaderView.ResizeMode.Stretch)
        for column in range(1,7):self.jobs_table.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeMode.ResizeToContents)
        tasks_layout.addWidget(self.jobs_table,1)
        footer=QHBoxLayout()
        refresh=QPushButton("刷新")
        refresh.clicked.connect(self.refresh_tasks)
        resume=QPushButton("继续所选任务")
        resume.clicked.connect(self.resume_job)
        pause=QPushButton("暂停当前任务")
        pause.clicked.connect(self.cancel_task)
        diagnostics=QPushButton("环境检测")
        diagnostics.clicked.connect(self.diagnostics)
        open_data=QPushButton("打开书库目录")
        open_data.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.library.root))))
        self.delete_jobs_button=QPushButton('删除所选任务记录')
        self.delete_jobs_button.clicked.connect(self.delete_task_records)
        for button in [refresh,resume,pause,self.delete_jobs_button,diagnostics,open_data]:
            footer.addWidget(button)
        footer.addStretch()
        tasks_layout.addLayout(footer)
        tabs.addTab(tasks_page,'任务记录')
        usage_page=QWidget();usage_layout=QVBoxLayout(usage_page)
        self.usage_table=QTableWidget(0,6)
        self.usage_table.setHorizontalHeaderLabels(['记录时间','作品','模型','输入 tokens','输出 tokens','估算费用'])
        self.usage_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.usage_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.usage_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.usage_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.usage_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        usage_layout.addWidget(self.usage_table,1)
        usage_layout.addWidget(heading('仅为本机保存的请求记录。删除后上方累计用量重新计算，云端账单与已生成内容保留。',True))
        usage_footer=QHBoxLayout()
        self.delete_usage_button=QPushButton('删除所选用量记录');self.delete_usage_button.clicked.connect(self.delete_usage_records)
        usage_refresh=QPushButton('刷新');usage_refresh.clicked.connect(self.refresh_tasks)
        usage_footer.addWidget(usage_refresh);usage_footer.addWidget(self.delete_usage_button);usage_footer.addStretch()
        usage_layout.addLayout(usage_footer);tabs.addTab(usage_page,'云端请求与用量')
        layout.addWidget(tabs,1)
        return page

    def navigate(self,index):
        if index!=1:
            self.reader.pause_auto_reading()
        if self.pure and index!=1:
            self.set_pure(False)
        if self.stack.currentIndex()==1:
            self.reader.flush_progress()
        self.stack.setCurrentIndex(index)
        self.sidebar.setVisible(index!=1 and not self.pure)
        for button,page_index in self.navigation:
            button.setChecked(page_index==index)
        if index==0:
            self.refresh_library()
        if index==2:
            self.refresh_tasks()

    def refresh_library(self,*_):
        if not hasattr(self,"book_list"):
            return
        selected=self.selected_book()
        self.library.restore_recycled_books()
        records=self.library.list_books(self.search.text(),["all","comic","novel"][self.kind_filter.currentIndex()],self.favorite_filter.isChecked())
        self.book_list.clear()
        for record in records:
            try:
                manifest=self.library.manifest(record["id"])
                count=len(manifest["units"])
                progress=int((record["progress"]+record["scroll"])/max(1,count)*100)
                unit="页" if record["kind"]=="comic" else "章"
                item=QListWidgetItem(("★ " if record["favorite"] else "")+record["title"][:40]+f"\n{count} {unit} · 阅读 {progress}%\n"+(record["tags"] or record["author"] or ("漫画" if unit=="页" else "小说")))
                item.setData(Qt.ItemDataRole.UserRole,record["id"])
                item.setSizeHint(QSize(200,315))
                item.setToolTip(record["title"]+"\n"+record["source"])
                pixmap=QPixmap()
                if manifest.get("cover"):
                    pixmap=QPixmap(str(self.library.path(record["id"])/manifest["cover"]))
                if pixmap.isNull():
                    from PySide6.QtGui import QPainter,QColor,QFont
                    pixmap=QPixmap(160,215)
                    pixmap.fill(QColor("#24473f"))
                    painter=QPainter(pixmap)
                    painter.setPen(QColor("#b8ecda"))
                    painter.setFont(QFont("Microsoft YaHei",20))
                    painter.drawText(pixmap.rect().adjusted(15,15,-15,-15),Qt.AlignmentFlag.AlignCenter|Qt.TextFlag.TextWordWrap,record["title"][:18])
                    painter.end()
                item.setIcon(QIcon(cover_thumbnail(pixmap)))
                self.book_list.addItem(item)
                if record["id"]==selected:
                    self.book_list.setCurrentItem(item)
            except (OSError,ValueError,KeyError):
                continue
        self.book_list.setVisible(bool(records))
        self.empty.setVisible(not records)
        self.count.setText(f"{len(records)} 部作品 · 双击封面继续阅读")

    def selected_book(self):
        item=self.book_list.currentItem() if hasattr(self,"book_list") else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def current_book(self):
        return self.reader.book_id if self.stack.currentIndex()==1 else self.selected_book()

    def open_selected(self):
        if self.selected_book():
            self.open_book(self.selected_book())

    def open_book(self,book_id):
        try:
            self.reader.open_book(book_id)
            self.navigate(1)
        except Exception as error:
            self.info("无法打开作品",str(error))

    def import_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,"导入漫画或小说","","漫画与小说 (*.cbz *.zip *.epub *.txt *.html *.htm *.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if paths:
            self.import_paths(paths)

    def import_folder(self):
        path=QFileDialog.getExistingDirectory(self,"选择漫画图片文件夹")
        if path:
            self.import_paths([path])

    def import_paths(self,paths):
        def work(progress,cancel):
            result=None
            for path in paths:
                if cancel():
                    raise InterruptedError("导入已暂停")
                result=import_book(self.library,Path(path),progress,cancel)
            return result
        self.start_task(work,self.imported,"正在导入作品")

    def imported(self,book_id):
        self.refresh_library()
        if book_id:
            self.open_book(book_id)

    def info(self,title,message):
        QMessageBox.information(self,title,message)

    def start_task(self,function,callback=None,message="正在处理"):
        if self.active_task and self.active_task.isRunning():
            self.info("任务正在进行","请等待当前任务完成，或先暂停当前任务。")
            return False
        task=Task(function,self)
        self.active_task=task
        self.task_label.setText(message)
        self.progress.setRange(0,0)
        self.progress.show()
        self.cancel_button.show()
        task.progress.connect(self.task_progress)
        task.succeeded.connect(lambda result:self.task_succeeded(result,callback))
        task.failed.connect(self.task_failed)
        task.paused.connect(self.task_paused)
        task.finished.connect(lambda:self.task_finished(task))
        task.start()
        return True

    def task_progress(self,done,total,message):
        self.task_label.setText(message)
        if total:
            self.progress.setRange(0,total)
            self.progress.setValue(done)

    def task_succeeded(self,result,callback):
        self.task_label.setText("任务完成")
        if callback:
            try:
                callback(result)
            except Exception as error:
                self.info("无法显示结果",str(error))

    def task_failed(self,message):
        self.task_label.setText("任务失败，已保留此前结果")
        self.info("任务未完成",message)

    def task_paused(self,message):
        self.task_label.setText(message)

    def task_finished(self,task):
        self.progress.hide()
        self.cancel_button.hide()
        self.active_task=None
        task.deleteLater()
        self.refresh_tasks()
        if self.closing:
            self.close()

    def cancel_task(self):
        if self.active_task:
            self.active_task.cancel()
            self.task_label.setText("正在暂停；当前网络请求结束后保存结果")

    def translate(self,whole=False,ocr_only=False,reprocess=False):
        book_id=self.current_book()
        if not book_id:
            self.info("选择作品","请先打开或选择一部作品。")
            return
        if not ocr_only and (not get_api_key() or not self.settings.model.strip()):
            self.show_settings()
            if not get_api_key() or not self.settings.model.strip():
                return
        manifest=self.library.manifest(book_id)
        indexes=list(range(len(manifest["units"]))) if whole or self.stack.currentIndex()!=1 else [self.reader.index]
        settings=replace(self.settings)
        self.start_task(lambda progress,cancel:translate_book(self.library,book_id,settings,indexes,progress,cancel,ocr_only=ocr_only,reprocess=reprocess),
                        self.translation_done,"正在识别文字" if ocr_only else "正在翻译作品")

    def translation_done(self,book_id):
        if self.reader.book_id==book_id:
            from .upscaling import available_edition
            version=3 if available_edition(self.library,book_id,self.reader.index,'translated') else 1
            if self.reader.colored.isChecked():
                from .colorization import available_color
                if available_color(self.library,book_id,self.reader.index,'upscaled_translated'):version=3
                elif available_color(self.library,book_id,self.reader.index,'translated'):version=1
            self.reader.select_edition('upscaled_translated' if version==3 else 'translated')
        self.refresh_library()

    def edit_current(self):
        self.reader.pause_auto_reading()
        if not self.reader.book_id:
            return
        if self.active_task and self.active_task.isRunning():
            self.info("任务进行中","请先暂停任务再编辑，避免覆盖正在生成的校对数据。")
            return
        book_id,index=self.reader.book_id,self.reader.index
        key=None
        if self.reader.manifest['kind']=='comic':
            from .edition_review import review_key,valid_image
            if self.reader.version.currentIndex()!=1:
                self.info('原版不提供校对','请切换到需要修正的中文版。');return
            key=review_key(self.reader.selected_edition(),self.reader.colored.isChecked())
            if not valid_image(self.library,book_id,index,key):
                self.info('当前版本尚未生成','请先生成对应的中文版、超分版或彩色版。');return
        dialog=EditorDialog(self.library,self.settings,book_id,index,self,colored=self.reader.colored.isChecked(),edition=self.reader.selected_edition(),review_key=key)
        dialog.render_requested.connect(lambda edits:self.render_current(book_id,index,edits,key))
        dialog.exec()
        self.reader.show_unit(self.reader.scroll_fraction())

    def render_current(self,book_id,index,edits,key=None):
        settings=replace(self.settings)
        if self.library.manifest(book_id)['kind']=='comic' and key is None:
            from .edition_review import review_key
            if self.reader.book_id==book_id and self.reader.version.currentIndex()!=1:
                self.info('原版不提供校对','请切换到需要修正的中文版。');return
            key=review_key(self.reader.selected_edition(),self.reader.colored.isChecked()) if self.reader.book_id==book_id else 'translated'
        def work(progress,cancel):
            from .edition_review import render_review,LABELS
            progress(0,1,'只更新当前'+LABELS[key]+'…')
            return render_review(self.library,book_id,index,key,edits,settings,cancel=cancel)
        def done(_):
            if self.reader.book_id==book_id:self.reader.show_unit(self.reader.scroll_fraction())
            self.refresh_library()
        self.start_task(work,done,"正在生成译图")

    def select_translation(self,index=None):
        self.reader.pause_auto_reading()
        if not self.reader.manifest or self.reader.manifest["kind"]!="comic":
            return
        if self.active_task and self.active_task.isRunning():
            self.info("任务进行中","请先暂停当前任务，再框选识别翻译。")
            return
        book_id=self.reader.book_id
        page=self.reader.index if index is None else index
        from .edition_review import ensure_page,review_key,valid_image
        ensure_page(self.library,book_id,page)
        edition=self.reader.selected_edition()
        key=review_key(edition.replace('original','translated'),self.reader.colored.isChecked())
        if not valid_image(self.library,book_id,page,key):
            if edition.endswith('translated'):
                self.info('当前版本尚未生成','请先生成当前中文版，再进行框选翻译。');return
            key='translated'
        dialog=SelectionTranslationDialog(self.library,replace(self.settings),book_id,page,self,colored=self.reader.colored.isChecked(),edition=edition,review_key=key)
        dialog.exec()
        if dialog.applied:
            if self.reader.index!=page:
                self.reader.jump(page)
            self.reader.show_unit(self.reader.scroll_fraction())
            self.refresh_library()
        self.refresh_tasks()

    def show_settings(self):
        self.reader.pause_auto_reading()
        if self.active_task and self.active_task.isRunning():
            self.info("任务进行中","请先暂停任务再修改翻译设置。")
            return
        dialog=SettingsDialog(self.settings,self.library,self)
        if dialog.exec():
            apply_appearance(QApplication.instance(),self.settings)
            if self.reader.manifest:self.reader.show_unit(self.reader.scroll_fraction())

    def upscale_current(self,page=None):
        self.reader.pause_auto_reading()
        if self.active_task and self.active_task.isRunning():
            self.info('任务进行中','请先暂停当前任务，再进行超分。');return
        book=self.current_book()
        if not book:return
        manifest=self.library.manifest(book)
        if manifest['kind']!='comic':
            self.info('漫画超分','超分仅用于漫画');return
        from .upscale_widgets import UpscaleDialog
        from .upscaling import upscale_book
        current=(self.reader.index if page is None else page) if self.stack.currentIndex()==1 else None
        source_kind=('translated' if self.reader.version.currentIndex()==1 else 'original') if current is not None else None
        dialog=UpscaleDialog(self.settings,len(manifest['units']),current,self,source_kind=source_kind)
        if not dialog.exec():return
        settings=replace(self.settings,**dialog.options.values());indexes=dialog.indexes()
        needs_translation=source_kind is None and settings.upscale_mode=='before'
        if needs_translation and (not get_api_key() or not settings.model.strip()):
            self.show_settings();settings=replace(self.settings,**dialog.options.values())
            if not get_api_key() or not settings.model.strip():return
        for key,value in dialog.options.values().items():
            if source_kind is None or key!='upscale_mode':setattr(self.settings,key,value)
        self.settings.save(self.library.root)
        def done(result):
            if self.reader.book_id==result:
                self.reader.select_edition('upscaled_original' if settings.upscale_mode=='only' else 'upscaled_translated')
            self.refresh_library()
        self.start_task(lambda progress,cancel:upscale_book(self.library,book,settings,indexes,progress,cancel,source_kind=source_kind),done,'正在超分')

    def color_current(self,page=None):
        self.reader.pause_auto_reading()
        if self.active_task and self.active_task.isRunning():
            self.info('任务进行中','请先暂停当前任务，再进行上色。');return
        book=self.current_book()
        if not book:return
        manifest=self.library.manifest(book)
        if manifest['kind']!='comic':return
        from .color_widgets import ColorDialog
        from .colorization import colorize_book,source_path
        current=(self.reader.index if page is None else page) if self.stack.currentIndex()==1 else None
        edition=self.reader.selected_edition() if current is not None else 'original'
        dialog=ColorDialog(self.settings,len(manifest['units']),current,edition,self,library=self.library,book=book)
        if not dialog.exec():return
        indexes=dialog.indexes();edition=dialog.edition.currentData()
        try:
            for index in indexes:source_path(self.library,book,index,edition)
        except ValueError as error:self.info('请先准备来源版本',str(error));return
        for key,value in dialog.options.values().items():setattr(self.settings,key,value)
        self.settings.save(self.library.root);settings=replace(self.settings)
        reference=dialog.reference.text().strip();force=dialog.force.isChecked()
        def done(result):
            if self.reader.book_id==result:
                self.reader.select_edition(edition,colored=True)
            self.refresh_library()
        self.start_task(lambda progress,cancel:colorize_book(self.library,book,settings,indexes,edition,reference,progress,cancel,force=force),done,'正在生成独立彩色版本')

    def book_menu(self,position):
        item=self.book_list.itemAt(position)
        if not item:
            return
        self.book_list.setCurrentItem(item)
        book_id=item.data(Qt.ItemDataRole.UserRole)
        menu=self.build_book_menu(book_id)
        menu.exec(self.book_list.mapToGlobal(position))
        menu.deleteLater()

    def build_book_menu(self,book_id):
        menu=QMenu(self)
        menu.addAction("打开阅读",lambda:self.open_book(book_id))
        menu.addAction("翻译整部作品",lambda:self.translate(True))
        if self.library.manifest(book_id)['kind']=='comic':menu.addAction('漫画超分…',lambda:self.upscale_current())
        if self.library.manifest(book_id)['kind']=='comic':menu.addAction('漫画上色…',lambda:self.color_current())
        menu.addAction("编辑作品信息",lambda:self.edit_metadata(book_id))
        record=self.library.book(book_id)
        menu.addAction("取消收藏" if record["favorite"] else "加入收藏",lambda:self.favorite(book_id))
        menu.addAction("导出译本",self.export_current)
        menu.addAction("打开作品目录",lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.library.path(book_id)))))
        menu.addSeparator()
        menu.addAction("删除作品…",lambda:self.delete_book(book_id))
        return menu

    def delete_book(self,book_id):
        if not book_id:
            self.info("请选择作品","先在书库中选择要删除的漫画或小说。")
            return
        if self.active_task and self.active_task.isRunning():
            self.info("任务正在进行","请等待当前任务完成，或先暂停任务后再删除作品。")
            return
        record=self.library.book(book_id)
        if not record:
            return
        self.reader.pause_auto_reading()
        dialog=QMessageBox(self)
        dialog.setWindowTitle("删除作品")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setText(f"将《{record['title']}》移入回收站？")
        dialog.setInformativeText("书库中的原图或小说正文、译图或译文、OCR 和校对数据将一起移入 Windows 回收站。\n"
                                  "导入前的源文件和另行导出的文件不会被删除。\n\n"
                                  "需要恢复时，在回收站还原该作品文件夹，再重新打开程序即可恢复作品和阅读进度。")
        dialog.setDetailedText(f"回收站文件夹名称：{book_id}\n原位置：{self.library.path(book_id)}")
        confirm=dialog.addButton("移入回收站",QMessageBox.ButtonRole.DestructiveRole)
        cancel=dialog.addButton("取消",QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        if dialog.clickedButton()!=confirm:
            return
        try:
            if self.reader.book_id==book_id:
                if self.stack.currentIndex()==1:
                    self.navigate(0)
                self.reader.clear_book()
            self.library.recycle_book(book_id)
        except Exception as error:
            self.refresh_library()
            self.info("删除未完成",str(error))
            return
        self.refresh_library()
        self.refresh_tasks()
        self.task_label.setText(f"《{record['title']}》已移入回收站")

    def favorite(self,book_id):
        self.library.update(book_id,favorite=1-self.library.book(book_id)["favorite"])
        self.refresh_library()

    def edit_metadata(self,book_id):
        record=self.library.book(book_id)
        dialog=QDialog(self)
        dialog.setWindowTitle("作品信息")
        form=QFormLayout(dialog)
        inputs={key:QLineEdit(record[key]) for key in ["title","author","tags"]}
        for key,label in [("title","名称"),("author","作者"),("tags","标签（逗号分隔）")]:
            form.addRow(label,inputs[key])
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() and inputs["title"].text().strip():
            self.library.update(book_id,**{key:widget.text().strip() for key,widget in inputs.items()})
            self.refresh_library()

    def export_current(self):
        book_id=self.current_book()
        if not book_id:
            self.info("选择作品","请先选择作品。")
            return
        if self.active_task and self.active_task.isRunning():
            self.info("任务进行中","请先暂停当前任务，再导出已有结果。")
            return
        manifest=self.library.manifest(book_id)
        dialog=QDialog(self)
        dialog.setWindowTitle("导出独立译本")
        form=QFormLayout(dialog)
        format_box=QComboBox()
        format_box.addItems(["CBZ 漫画","图片文件夹"] if manifest["kind"]=="comic" else ["EPUB 电子书","TXT 文本"])
        edition=QComboBox();edition.addItems(['普通中文版','原版超分','中文版超分','彩色原版','彩色中文版','彩色原版超分','彩色中文版超分'])
        if manifest['kind']=='comic':form.addRow('导出版本',edition)
        scope=QComboBox()
        scope.addItems(["整部作品","当前页 / 章","指定范围"])
        if self.stack.currentIndex()!=1:
            scope.model().item(1).setEnabled(False)
        start,end=QSpinBox(),QSpinBox()
        for widget in [start,end]:
            widget.setRange(1,len(manifest["units"]))
        end.setValue(len(manifest["units"]))
        form.addRow("格式",format_box)
        form.addRow("范围",scope)
        form.addRow("起始页 / 章",start)
        form.addRow("结束页 / 章",end)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if not dialog.exec():
            return
        indexes=list(range(len(manifest["units"]))) if scope.currentIndex()==0 else [self.reader.index] if scope.currentIndex()==1 else list(range(start.value()-1,end.value()))
        if not indexes:
            self.info("范围不正确","起始页不能大于结束页。")
            return
        format=["cbz","folder"][format_box.currentIndex()] if manifest["kind"]=="comic" else ["epub","txt"][format_box.currentIndex()]
        title=safe_name(self.library.book(book_id)["title"])+"_中文译本"
        if manifest['kind']=='comic' and edition.currentIndex()>0:title=safe_name(self.library.book(book_id)['title'])+'_'+edition.currentText()
        if format=="folder":
            parent=QFileDialog.getExistingDirectory(self,"选择译图保存位置")
            path=str(Path(parent)/(title+time.strftime("_%Y%m%d_%H%M%S"))) if parent else ""
        else:
            path,_=QFileDialog.getSaveFileName(self,"保存译本",title+"."+format,f"译本 (*.{format})")
            if path and not path.lower().endswith("."+format):
                path+="."+format
        if path:
            selected_edition=['translated','upscaled_original','upscaled_translated','colored_original','colored_translated','colored_upscaled_original','colored_upscaled_translated'][edition.currentIndex()]
            self.start_task(lambda progress,cancel:export_book(self.library,book_id,Path(path),format,indexes,progress,cancel,edition=selected_edition),
                            lambda result:self.info("译本已保存",result),"正在导出译本")

    def backup(self):
        path,_=QFileDialog.getSaveFileName(self,"备份书库","阅译书库备份.zip","ZIP (*.zip)")
        if path:
            self.start_task(lambda progress,cancel:backup_library(self.library,Path(path),progress,cancel),
                            lambda result:self.info("备份完成",result),"正在备份书库")

    def refresh_tasks(self):
        if not hasattr(self,"jobs_table"):
            return
        usage=self.library.usage()
        self.usage_label.setText(f"已记录 {usage['requests']} 次云端请求 · 输入 {usage['input_tokens']:,} tokens · 输出 {usage['output_tokens']:,} tokens · 按设置单价估算 {usage['cost']:.4f}")
        jobs=self.library.jobs()
        self.jobs_table.setRowCount(len(jobs))
        types={"translate":"翻译","ocr":"识别","download":"下载（历史）","upscale":"超分","colorize":"上色"}
        states={"running":"进行中","paused":"已暂停","failed":"待重试","complete":"完成"}
        for row,job in enumerate(jobs):
            values=[job["title"] or ('网站下载' if job['kind']=='download' else '已删除作品 / 未关联作品'),types.get(job["kind"],job["kind"]),states.get(job["status"],job["status"]),
                    f"{job['done']}/{job['total']}",format_timestamp(job.get('created')),
                    format_timestamp(job.get('finished'),'—'),format_timestamp(job['updated']),job["message"]]
            for column,value in enumerate(values):
                item=QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole,job)
                self.jobs_table.setItem(row,column,item)
        records=self.library.usage_records();self.usage_table.setRowCount(len(records))
        for row,record in enumerate(records):
            values=[format_timestamp(record['created']),record['title'] or '已删除作品 / 未关联作品',
                    record['model'],f"{record['input_tokens'] or 0:,}",f"{record['output_tokens'] or 0:,}",f"{record['cost'] or 0:.6f}"]
            for column,value in enumerate(values):
                item=QTableWidgetItem(str(value));item.setData(Qt.ItemDataRole.UserRole,record)
                self.usage_table.setItem(row,column,item)

    @staticmethod
    def selected_records(table):
        return [table.item(index.row(),0).data(Qt.ItemDataRole.UserRole) for index in table.selectionModel().selectedRows()]

    def delete_task_records(self):
        records=self.selected_records(self.jobs_table)
        if not records:return
        if any(r['status']=='running' for r in records):
            self.info('任务进行中','请先暂停所选任务，等待保存完成后再删除记录。');return
        reply=QMessageBox.question(self,'删除任务记录',f'删除所选 {len(records)} 条任务记录？\n\n原图、译图、校对内容及作品文件保留。删除暂停或失败记录后，无法从这条记录续做，可以重新发起任务。',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if reply!=QMessageBox.StandardButton.Yes:return
        try:count=self.library.delete_jobs([r['id'] for r in records])
        except (ValueError,OSError,sqlite3.Error) as error:self.info('无法删除记录',str(error));self.refresh_tasks();return
        self.refresh_tasks();self.task_label.setText(f'已删除 {count} 条任务记录；作品内容保留')

    def delete_usage_records(self):
        records=self.selected_records(self.usage_table)
        if not records:return
        reply=QMessageBox.question(self,'删除用量记录',f'删除所选 {len(records)} 条本地用量记录？\n\n累计请求次数、tokens 与估算费用会重新计算。云端账单、译文及翻译缓存保留。',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if reply!=QMessageBox.StandardButton.Yes:return
        try:count=self.library.delete_usage_records([r['id'] for r in records])
        except (ValueError,OSError,sqlite3.Error) as error:self.info('无法删除记录',str(error));self.refresh_tasks();return
        self.refresh_tasks();self.task_label.setText(f'已删除 {count} 条本地用量记录')

    def resume_job(self):
        selected=self.selected_records(self.jobs_table)
        if not selected:return
        if len(selected)>1:
            self.info('选择一个任务','请只选择一个暂停或失败任务继续。');return
        if self.active_task and self.active_task.isRunning():
            self.info("任务进行中","请先等待或暂停当前任务。")
            return
        job=selected[0]
        if job["status"]=="complete":
            self.info("任务已完成","请选择暂停或失败的任务。")
            return
        if job["kind"]=="download":
            self.info("功能已移除","获取作品功能已移除。这条下载记录仅供查看或删除；已有作品仍可正常阅读。")
            return
        payload=json.loads(job["payload"])
        if job['kind']=='colorize':
            from .colorization import colorize_book,COLOR_FIELDS
            from .upscaling import fingerprint
            from .config import get_color_api_key,get_openai_color_api_key
            try:
                settings=replace(self.settings,**{k:v for k,v in payload['options'].items() if k in COLOR_FIELDS})
                key_reader=get_openai_color_api_key if settings.color_engine=='openai' else get_color_api_key
                if settings.color_engine in ('nano','openai') and not key_reader():
                    self.show_settings()
                    if not key_reader():return
                reference=payload.get('reference','')
                if reference:
                    reference=(self.library.path(job['book_id'])/reference).resolve()
                    if not reference.is_relative_to((self.library.path(job['book_id'])/'colored/references').resolve()) or fingerprint(reference)!=payload['reference_hash']:
                        raise ValueError('任务参考图不存在或已改变，请重新发起上色任务')
            except (OSError,ValueError,KeyError) as error:self.info('无法续做上色',str(error));return
            self.start_task(lambda progress,cancel:colorize_book(self.library,job['book_id'],settings,payload['indexes'],payload['edition'],reference,progress,cancel,
                            job_id=job['id'],force=payload.get('force',False)),
                            lambda book:self.reader.show_unit(self.reader.scroll_fraction()) if self.reader.book_id==book else None,'继续上色')
            return
        if job['kind']=='upscale':
            from .upscaling import upscale_book
            settings=replace(self.settings,**{'upscale_'+key:value for key,value in payload['options'].items()})
            self.library.update_job(job['id'],job['done'],'running','继续超分')
            self.start_task(lambda progress,cancel:upscale_book(self.library,job['book_id'],settings,payload['indexes'],progress,cancel,job_id=job['id'],source_kind=payload.get('source_kind')),
                            lambda book:self.reader.show_unit(self.reader.scroll_fraction()) if self.reader.book_id==book else None,'继续超分')
            return
        settings=replace(self.settings)
        if job["kind"] in {"translate","ocr"}:
            if job["kind"]=="translate" and (not get_api_key() or not settings.model.strip()):
                self.show_settings()
                settings=replace(self.settings)
                if not get_api_key() or not settings.model.strip():
                    return
            self.library.update_job(job["id"],job["done"],"running","继续任务")
            self.start_task(lambda progress,cancel:translate_book(self.library,job["book_id"],settings,payload["indexes"],progress,cancel,
                            job_id=job["id"],ocr_only=job["kind"]=="ocr",reprocess=payload.get('reprocess',False)),self.translation_done,"继续任务")

    def diagnostics(self):
        def work(progress,cancel):
            import torch
            return f"Python {sys.version.split()[0]}\nPyTorch {torch.__version__}\nGPU 加速：{'可用' if torch.cuda.is_available() else '当前为 CPU 模式'}\n"+(torch.cuda.get_device_name(0) if torch.cuda.is_available() else "可按说明安装支持 RTX 50 系列的 PyTorch 构建。")
        self.start_task(work,lambda result:self.info("环境检测",result),"正在检查环境")

    def reader_step(self,offset):
        if self.stack.currentIndex()==1:
            self.reader.step(offset)

    def toggle_auto_reader(self):
        if self.stack.currentIndex()==1 and not QApplication.activeModalWidget():
            self.reader.toggle_auto_reading()

    def direction_step(self,key_direction):
        if self.stack.currentIndex()==1:
            self.reader.step(-key_direction if self.settings.reading_direction=="rtl" else key_direction)

    def reader_menu(self,position):
        page=self.reader.index
        if self.reader._active_continuous:
            point=self.reader.continuous_canvas.mapFromGlobal(self.reader.mapToGlobal(position))
            page=self.reader.continuous_canvas.position(point.y())[0]
        menu=self.build_reader_menu(page)
        menu.exec(self.reader.mapToGlobal(position))
        menu.deleteLater()

    def build_reader_menu(self,page=None):
        menu=QMenu(self)
        menu.addAction("上一页 / 章",lambda:self.reader.step(-1))
        menu.addAction("下一页 / 章",lambda:self.reader.step(1))
        menu.addSeparator()
        menu.addAction("显示原版",lambda:self.reader.version.setCurrentIndex(0))
        menu.addAction("显示中文版",lambda:self.reader.version.setCurrentIndex(1))
        if self.reader.manifest and self.reader.manifest["kind"]=="comic":
            menu.addAction("框选识别与翻译…",lambda:self.select_translation(page))
            menu.addAction('漫画超分…',lambda:self.upscale_current(page))
            menu.addAction('漫画上色…',lambda:self.color_current(page))
            menu.addAction("暂停自动阅读  F8" if self.reader.auto_running else "开始自动阅读  F8",self.reader.toggle_auto_reading)
            menu.addAction("自动阅读设置",self.reader.show_auto_settings)
            mode_menu=menu.addMenu("漫画阅读模式")
            for label,index in [("单页阅读",0),("整本连续预览",1)]:
                action=mode_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.reader.reading_mode.currentIndex()==index)
                action.triggered.connect(lambda checked,i=index:self.reader.reading_mode.setCurrentIndex(i))
            wheel=menu.addAction("滚轮上下翻页（单页模式）")
            wheel.setCheckable(True)
            wheel.setChecked(self.reader.wheel_paging.isChecked())
            wheel.setEnabled(not self.reader._active_continuous)
            wheel.triggered.connect(self.reader.wheel_paging.setChecked)
        for label,index in [("适应宽度",0),("整页显示",1),("原始尺寸",2),("双页显示",3)]:
            action=menu.addAction(label,lambda i=index:self.reader.fit.setCurrentIndex(i))
            action.setEnabled(not (index==3 and self.reader._active_continuous))
        menu.addSeparator()
        menu.addAction("翻译本页 / 章",lambda:self.translate(False))
        menu.addAction("翻译整部作品",lambda:self.translate(True))
        menu.addAction("仅识别漫画文字",lambda:self.translate(False,True))
        editor_action=menu.addAction("校对与排版",self.edit_current)
        editor_action.setEnabled(not self.reader.manifest or self.reader.manifest['kind']!='comic' or self.reader.version.currentIndex()==1)
        menu.addAction("导出译本",self.export_current)
        menu.addSeparator()
        menu.addAction("退出无边框" if self.pure else "无边框阅读  F10",self.toggle_pure)
        top=menu.addAction("窗口置顶")
        top.setCheckable(True)
        top.setChecked(self.settings.topmost)
        top.triggered.connect(self.toggle_topmost)
        menu.addAction("全屏  F11",self.toggle_fullscreen)
        menu.addAction("设置",self.show_settings)
        menu.addAction("返回书库",lambda:self.navigate(0))
        return menu

    def toggle_pure(self):
        if not self.reader.book_id or self.stack.currentIndex()!=1:
            self.info("先打开作品","打开作品后按 F10 进入无边框阅读。")
            return
        self.set_pure(not self.pure)

    def set_pure(self,pure):
        self.finish_pure_drag()
        geometry=self.geometry()
        fullscreen=self.isFullScreen()
        self.pure=pure
        self.settings.frameless=pure
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint,pure)
        self.reader.set_pure(pure)
        self.sidebar.setVisible(not pure and self.stack.currentIndex()!=1)
        self.status.setVisible(not pure)
        self.setMinimumSize(240,180) if pure else self.setMinimumSize(700,500)
        self.showFullScreen() if fullscreen else self.show()
        self.setGeometry(geometry)
        self.settings.save(self.library.root)

    def toggle_topmost(self,checked):
        geometry=self.geometry()
        fullscreen=self.isFullScreen()
        self.settings.topmost=checked
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,checked)
        self.showFullScreen() if fullscreen else self.show()
        self.setGeometry(geometry)
        self.settings.save(self.library.root)

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def escape(self):
        if self.isFullScreen():
            self.showNormal()
        if self.pure:
            self.set_pure(False)

    def finish_pure_drag(self):
        if self.pure_drag:
            self.releaseMouse()
            self.pure_drag=None
        self.space_held=False

    def pure_edges(self,position):
        point=self.mapFromGlobal(position)
        edges=Qt.Edge(0)
        if point.x()<7:edges|=Qt.Edge.LeftEdge
        elif point.x()>=self.width()-7:edges|=Qt.Edge.RightEdge
        if point.y()<7:edges|=Qt.Edge.TopEdge
        elif point.y()>=self.height()-7:edges|=Qt.Edge.BottomEdge
        return edges

    def update_pure_drag(self,position):
        origin,geometry,edges=self.pure_drag
        delta=position-origin
        rectangle=QRect(geometry)
        if not edges:
            rectangle.moveTopLeft(geometry.topLeft()+delta)
        else:
            if edges & Qt.Edge.LeftEdge:
                rectangle.setLeft(min(geometry.left()+delta.x(),geometry.right()-self.minimumWidth()+1))
            if edges & Qt.Edge.RightEdge:
                rectangle.setRight(max(geometry.right()+delta.x(),geometry.left()+self.minimumWidth()-1))
            if edges & Qt.Edge.TopEdge:
                rectangle.setTop(min(geometry.top()+delta.y(),geometry.bottom()-self.minimumHeight()+1))
            if edges & Qt.Edge.BottomEdge:
                rectangle.setBottom(max(geometry.bottom()+delta.y(),geometry.top()+self.minimumHeight()-1))
        self.setGeometry(rectangle)

    def eventFilter(self,obj,event):
        if self.pure and QApplication.activeWindow()==self:
            if event.type()==QEvent.Type.KeyPress and event.key()==Qt.Key.Key_Space:
                self.space_held=True
                return True
            if event.type()==QEvent.Type.KeyRelease and event.key()==Qt.Key.Key_Space:
                self.space_held=False
                return True
            if event.type()==QEvent.Type.MouseButtonPress and event.button()==Qt.MouseButton.LeftButton:
                if isinstance(obj,QWidget) and (obj==self or self.isAncestorOf(obj)):
                    edges=self.pure_edges(event.globalPosition().toPoint())
                    if (edges or self.space_held) and not self.isFullScreen() and not self.isMaximized():
                        self.pure_drag=(event.globalPosition().toPoint(),self.geometry(),Qt.Edge(0) if self.space_held else edges)
                        self.grabMouse()
                        return True
            if event.type()==QEvent.Type.MouseMove and self.pure_drag:
                self.update_pure_drag(event.globalPosition().toPoint())
                return True
            if event.type()==QEvent.Type.MouseButtonRelease and self.pure_drag:
                self.update_pure_drag(event.globalPosition().toPoint())
                self.finish_pure_drag()
                return True
            if event.type()==QEvent.Type.WindowDeactivate:
                self.finish_pure_drag()
        return super().eventFilter(obj,event)

    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self,event):
        self.import_paths([url.toLocalFile() for url in event.mimeData().urls()])
        event.acceptProposedAction()

    def closeEvent(self,event):
        self.reader.pause_auto_reading()
        self.reader.flush_progress()
        if self.active_task and self.active_task.isRunning():
            self.closing=True
            self.cancel_task()
            self.task_label.setText("正在保存并退出，请稍候")
            event.ignore()
            return
        from .render_pool import close_render_pool
        close_render_pool()
        self.settings.geometry=bytes(self.saveGeometry().toBase64()).decode()
        self.settings.save(self.library.root)
        self.reader.continuous_canvas.release()
        self.reader.continuous_canvas.pool.waitForDone()
        super().closeEvent(event)


def run():
    application=QApplication(sys.argv)
    application.setApplicationName("阅译")
    application.setOrganizationName("ReadTranslate")
    application.setStyle("Fusion")
    application.setStyleSheet(STYLE)
    assets=Path(getattr(sys,"_MEIPASS",Path(__file__).resolve().parent.parent))/"assets"
    application.setWindowIcon(QIcon(str(assets/"icon.ico")))
    lock=QLockFile(str(data_root()/"application.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None,"阅译已在运行","同一书库已经在其他窗口中打开，请切换到该窗口。")
        return 0
    window=MainWindow()
    window.show()
    return application.exec()
