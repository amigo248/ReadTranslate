"""Shared super-resolution controls for settings and per-book tasks."""
from PySide6.QtWidgets import QWidget,QFormLayout,QComboBox,QLabel,QDialog,QDialogButtonBox,QSpinBox
from .upscaling import ENGINES,MODES

class UpscaleOptions(QWidget):
    def __init__(self,settings,parent=None):
        super().__init__(parent);form=QFormLayout(self)
        form.setContentsMargins(0,0,0,0)
        self.engine=QComboBox();self.ratio=QComboBox();self.mode=QComboBox();self.tile=QComboBox()
        for text,data in ENGINES:self.engine.addItem(text,data)
        for ratio in (2,3,4):self.ratio.addItem(f'{ratio} 倍',ratio)
        for text,data in MODES:self.mode.addItem(text,data)
        for tile in (128,256,400):self.tile.addItem(f'{tile} 像素',tile)
        for field in ('engine','ratio','mode','tile'):
            widget=getattr(self,field);widget.setCurrentIndex(max(0,widget.findData(getattr(settings,'upscale_'+field))))
        for text,widget in [('超分方案',self.engine),('放大倍率',self.ratio),('处理方式',self.mode),('分块大小',self.tile)]:form.addRow(text,widget)
        self.note=QLabel('生成独立超分版本，原图保留。仅超分不识别、不翻译；含翻译的任务按现有接口计费。已有校对和框选继续保留。MangaJaNai 的 3 倍由 4 倍模型处理后缩回；彩图使用 IllustrationJaNai。Real-ESRGAN 使用显卡 Vulkan；另两种可使用 GPU 或 CPU。')
        self.note.setWordWrap(True);form.addRow(self.note)
    def values(self):return {'upscale_'+name:getattr(self,name).currentData() for name in ('engine','ratio','mode','tile')}

class UpscaleDialog(QDialog):
    def __init__(self,settings,count,current=None,parent=None,*,source_kind=None):
        super().__init__(parent);self.setWindowTitle('漫画超分');self.resize(570,480)
        form=QFormLayout(self);self.options=UpscaleOptions(settings);form.addRow(self.options)
        self.source_kind=source_kind
        if source_kind is not None:
            if source_kind not in ('original','translated'):raise ValueError('超分来源无效')
            self.options.mode.setCurrentIndex(self.options.mode.findData('only' if source_kind=='original' else 'before'))
            self.options.mode.setItemText(self.options.mode.currentIndex(),'仅超分原版' if source_kind=='original' else '使用已有中文译文生成超分版')
            self.options.mode.setEnabled(False)
            self.options.mode.setToolTip('来源跟随阅读界面左侧选择；不重新检测、OCR 或请求翻译')
            self.options.note.setText('生成独立超分版本，保留原图及现有译文。来源跟随阅读界面，不重复 OCR 或请求翻译，也不对已超分图重复放大。彩色超分版需再点击“上色”生成。MangaJaNai 的 3 倍由 4 倍模型处理后缩回；Real-ESRGAN 使用 Vulkan，另两种可使用 GPU 或 CPU。')
        self.scope=QComboBox();self.scope.addItems(['当前页','整本漫画','指定范围'])
        self.scope.model().item(0).setEnabled(current is not None)
        self.scope.setCurrentIndex(0 if current is not None else 1)
        self.start=QSpinBox();self.end=QSpinBox()
        for widget in (self.start,self.end):widget.setRange(1,count)
        self.start.setValue((current or 0)+1);self.end.setValue(count)
        form.addRow('处理范围',self.scope);form.addRow('起始页',self.start);form.addRow('结束页',self.end)
        def update_scope():
            for widget in (self.start,self.end):widget.setEnabled(self.scope.currentIndex()==2)
        self.scope.currentIndexChanged.connect(update_scope);update_scope()
        self.error=QLabel();self.error.setWordWrap(True);form.addRow(self.error)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('开始处理')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.validate);buttons.rejected.connect(self.reject);form.addRow(buttons)
        self.count,self.current=count,current
    def validate(self):
        if self.scope.currentIndex()==2 and self.start.value()>self.end.value():self.error.setText('起始页不能大于结束页');return
        self.accept()
    def indexes(self):
        return [self.current] if self.scope.currentIndex()==0 else list(range(self.count)) if self.scope.currentIndex()==1 else list(range(self.start.value()-1,self.end.value()))
