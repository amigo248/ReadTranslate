"""Optional color settings and explicit per-book task controls."""
from dataclasses import replace
from pathlib import Path
import time
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget,QFormLayout,QComboBox,QLabel,QDialog,QDialogButtonBox,
    QSpinBox,QLineEdit,QCheckBox,QDoubleSpinBox,QPushButton,QHBoxLayout,QFileDialog,QMessageBox,
    QVBoxLayout,QScrollArea,QPlainTextEdit,QListWidget,QAbstractItemView)
from .config import get_color_api_key,set_color_api_key,get_openai_color_api_key,set_openai_color_api_key
from .colorization import EDITIONS
from .color_api import ImageAPI,DEFAULT_PROMPT,sanitize


class ModelDialog(QDialog):
    def __init__(self,models,current='',parent=None):
        super().__init__(parent);self.setWindowTitle('选择上色模型');self.resize(540,500)
        layout=QVBoxLayout(self);self.search=QLineEdit();self.search.setPlaceholderText('搜索服务商返回的模型…')
        layout.addWidget(self.search);self.list=QListWidget();self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.addItems(models);layout.addWidget(self.list)
        note=QLabel('模型列表包含文字、识图和生图模型，请选择支持图片输出的模型。');note.setWordWrap(True);layout.addWidget(note)
        self.buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);layout.addWidget(self.buttons)
        self.buttons.accepted.connect(self.accept);self.buttons.rejected.connect(self.reject)
        self.list.itemDoubleClicked.connect(lambda _:self.accept())
        self.search.textChanged.connect(self.filter);self.list.currentItemChanged.connect(self.selection_changed)
        matches=self.list.findItems(current,Qt.MatchFlag.MatchExactly)
        if matches:self.list.setCurrentItem(matches[0])
        self.filter('')

    def selection_changed(self,*_):
        item=self.list.currentItem();self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(item and not item.isHidden()))

    def filter(self,text):
        visible=[]
        for index in range(self.list.count()):
            item=self.list.item(index);item.setHidden(text.casefold() not in item.text().casefold())
            if not item.isHidden():visible.append(item)
        if len(visible)==1:self.list.setCurrentItem(visible[0])
        self.selection_changed()

    def selected_model(self):
        item=self.list.currentItem();return item.text() if item and not item.isHidden() else ''

    def accept(self):
        if self.selected_model():super().accept()


class ColorOptions(QWidget):
    def __init__(self,settings,parent=None):
        super().__init__(parent);self.settings=settings;self.form=QFormLayout(self)
        self.engine=QComboBox();self.engine.addItem('Klein 4B · 本地','klein');self.engine.addItem('Gemini / Nano Banana · 云端','nano')
        self.engine.addItem('OpenAI 兼容图片 API · 云端','openai')
        self.engine.setCurrentIndex(max(0,self.engine.findData(settings.color_engine)))
        self.form.addRow('上色模型',self.engine)
        self.local_fields={};self.cloud_fields={};self.openai_fields={}
        for label,name in [('ComfyUI 地址','comfy_url'),('Klein 模型文件','klein_model'),
                           ('文本编码器文件','klein_encoder'),('VAE 文件','klein_vae')]:
            field=QLineEdit(getattr(settings,'color_'+name));self.local_fields[name]=field;self.form.addRow(label,field)
        self.check=QPushButton('检测 Klein 连接与模型');self.check.clicked.connect(self.check_local);self.form.addRow(self.check)
        for label,name in [('Gemini 接口地址','nano_url'),('Nano Banana 模型','nano_model')]:
            field=QLineEdit(getattr(settings,'color_'+name));self.cloud_fields[name]=field;self.form.addRow(label,field)
        self.key=QLineEdit(get_color_api_key());self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText('Nano Banana 上色密钥，与文字翻译密钥分开');self.form.addRow('Nano Banana 密钥',self.key)
        self.persist=QCheckBox('将上色密钥保存到 Windows 凭据管理器');self.form.addRow(self.persist)
        self.price=QDoubleSpinBox();self.price.setRange(0,10000);self.price.setDecimals(6);self.price.setValue(settings.color_nano_price)
        self.price.setSpecialValueText('未设置，无法估算');self.form.addRow('每页费用估算（美元）',self.price)
        for label,name in [('OpenAI 接口地址','openai_url'),('OpenAI 图像模型','openai_model')]:
            field=QLineEdit(getattr(settings,'color_'+name));self.openai_fields[name]=field;self.form.addRow(label,field)
        self.openai_quality=QComboBox()
        for label,value in [('自动','auto'),('低 · 预览','low'),('中 · 默认','medium'),('高 · 精细','high')]:self.openai_quality.addItem(label,value)
        self.openai_quality.setCurrentIndex(max(0,self.openai_quality.findData(settings.color_openai_quality)))
        self.form.addRow('OpenAI 上色画质',self.openai_quality)
        self.interface=QComboBox()
        for label,value in [('自动（仅接口不存在时切换）','auto'),('图片编辑 /images/edits','edits'),('图生图 /images/generations','generations'),('多模态 /chat/completions','chat')]:self.interface.addItem(label,value)
        self.interface.setCurrentIndex(max(0,self.interface.findData(settings.color_openai_interface)))
        self.form.addRow('OpenAI 接口类型',self.interface)
        self.openai_key=QLineEdit(get_openai_color_api_key());self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key.setPlaceholderText('独立 OpenAI API 密钥');self.form.addRow('OpenAI 上色密钥',self.openai_key)
        self.openai_persist=QCheckBox('将 OpenAI 上色密钥保存到 Windows 凭据管理器');self.form.addRow(self.openai_persist)
        self.openai_price=QDoubleSpinBox();self.openai_price.setRange(0,10000);self.openai_price.setDecimals(6);self.openai_price.setValue(settings.color_openai_price)
        self.openai_price.setSpecialValueText('未设置，无法估算');self.form.addRow('OpenAI 每页估算（美元）',self.openai_price)
        self.size=QComboBox()
        self.size.addItem('保留来源尺寸（不缩小）',0)
        for n in (768,1024,1536,2048,3072,4096):self.size.addItem(f'{n} 像素',n)
        if self.size.findData(settings.color_max_side)<0:self.size.addItem(f'{settings.color_max_side} 像素',settings.color_max_side)
        self.size.setCurrentIndex(max(0,self.size.findData(settings.color_max_side)));self.form.addRow('上传图片最长边',self.size)
        self.output_mode=QComboBox();self.output_mode.addItem('auto · 跟随实际上传尺寸','auto');self.output_mode.addItem('自定义','custom')
        self.form.addRow('请求生成尺寸',self.output_mode)
        self.output_custom=QWidget();custom=QHBoxLayout(self.output_custom);custom.setContentsMargins(0,0,0,0)
        self.output_width=QSpinBox();self.output_height=QSpinBox()
        for field in (self.output_width,self.output_height):field.setRange(1,8192);field.setSuffix(' 像素')
        for field in (QLabel('宽'),self.output_width,QLabel('高'),self.output_height):custom.addWidget(field)
        self.form.addRow('自定义宽高',self.output_custom)
        self._resolution_values={name:getattr(settings,'color_'+name+'_size') for name in ('openai','nano','klein')}
        self._resolution_engine=None
        self.output_mode.currentIndexChanged.connect(self.update_resolution)
        self.size_note=QLabel('auto 明确请求与实际上传画布相同的宽高，不发送服务商的 auto 参数。自定义宽高须符合模型限制；不同图片比例会提示。Gemini 只支持固定像素档位，不匹配时提示，绝不自动选更小档位。聊天接口不能精确指定尺寸。Klein 的宽高须为 16 的倍数，8GB 显存建议 1024 或更低。')
        self.size_note.setWordWrap(True);self.form.addRow(self.size_note)
        self.denoise=QSpinBox();self.denoise.setRange(-1,255);self.denoise.setSpecialValueText('关闭（-1）')
        self.denoise.setValue(settings.color_denoise_sigma);self.form.addRow('上色降噪强度',self.denoise)
        self.denoise_note=QLabel('上色前使用 MTU 同款 FFDNet 本地降噪，适用于全部上色引擎。0–255，越大越平滑；可先尝试 30。过强会抹掉线条和网点，默认关闭。')
        self.denoise_note.setWordWrap(True);self.form.addRow(self.denoise_note)
        self.square=QCheckBox('补白为方图；上色后核验补白和画面比例再还原');self.square.setChecked(settings.color_square);self.form.addRow(self.square)
        self.history=QSpinBox();self.history.setRange(0,5);self.history.setValue(settings.color_history_pages);self.form.addRow('附带前面已上色页数',self.history)
        self.prompt=QPlainTextEdit(settings.color_prompt);self.prompt.setPlaceholderText(DEFAULT_PROMPT);self.prompt.setMaximumHeight(140)
        self.form.addRow('上色提示词（留空使用默认）',self.prompt)
        self.network=QComboBox();self.network.addItem('直连（不使用系统代理）','direct');self.network.addItem('系统/环境代理','system')
        self.network.setCurrentIndex(max(0,self.network.findData(settings.color_network)));self.form.addRow('云端网络方式',self.network)
        self.timeout=QSpinBox();self.timeout.setRange(30,1200);self.timeout.setSuffix(' 秒');self.timeout.setValue(settings.color_timeout);self.form.addRow('上色请求超时',self.timeout)
        self.api_buttons=QWidget();row=QHBoxLayout(self.api_buttons);row.setContentsMargins(0,0,0,0)
        self.connection=QPushButton('连接测试');self.models=QPushButton('获取模型');self.image_test=QPushButton('图片上色测试（可能收费）')
        for button in (self.connection,self.models,self.image_test):row.addWidget(button)
        self.connection.clicked.connect(lambda:self.start_api('connection'));self.models.clicked.connect(lambda:self.start_api('models'))
        self.image_test.clicked.connect(self.confirm_image_test);self.form.addRow(self.api_buttons)
        self.cancel_test=QPushButton('取消检测');self.cancel_test.clicked.connect(self.cancel_api);self.cancel_test.setVisible(False);self.form.addRow(self.cancel_test)
        self.note=QLabel('仅对所选版本的图片上色并另存模型结果；不进行检测、OCR、翻译、修复、文字渲染或超分。Klein 需要已启动的 ComfyUI 和匹配模型，8GB 显存建议 1024 像素或更低。云端会上传图片及参考图，费用以服务商账单为准。后续手动校对为独立操作，不再调用上色模型。')
        self.note.setWordWrap(True);self.form.addRow(self.note)
        self.status=QLabel();self.status.setWordWrap(True);self.form.addRow(self.status);self.task=None
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.engine.currentIndexChanged.connect(self.update_mode);self.update_mode()

    def update_mode(self):
        engine=self.engine.currentData();local=engine=='klein'
        self.store_resolution()
        self._resolution_engine=engine
        value=self._resolution_values[engine]
        if value in ('1K','2K','4K'):value={'1K':'1024x1024','2K':'2048x2048','4K':'4096x4096'}[value]
        mode='auto' if value=='auto' else 'custom'
        width,height=(1024,1024) if mode=='auto' else tuple(map(int,value.split('x')))
        for field in (self.output_mode,self.output_width,self.output_height):field.blockSignals(True)
        self.output_mode.setCurrentIndex(self.output_mode.findData(mode));self.output_width.setValue(width);self.output_height.setValue(height)
        for field in (self.output_mode,self.output_width,self.output_height):field.blockSignals(False)
        self.update_resolution()
        for widget in list(self.local_fields.values())+[self.check]:self.form.setRowVisible(widget,local)
        for widget in list(self.cloud_fields.values())+[self.key,self.persist,self.price]:self.form.setRowVisible(widget,engine=='nano')
        for widget in list(self.openai_fields.values())+[self.openai_quality,self.interface,self.openai_key,self.openai_persist,self.openai_price]:self.form.setRowVisible(widget,engine=='openai')
        for widget in (self.square,self.history,self.network,self.timeout,self.api_buttons):self.form.setRowVisible(widget,not local)

    def update_resolution(self,*_):
        self.output_custom.setEnabled(self.output_mode.currentData()=='custom')

    def store_resolution(self):
        if self._resolution_engine:
            self._resolution_values[self._resolution_engine]='auto' if self.output_mode.currentData()=='auto' else f'{self.output_width.value()}x{self.output_height.value()}'

    def values(self):
        self.store_resolution()
        values={'color_engine':self.engine.currentData(),'color_nano_price':self.price.value(),
                'color_openai_price':self.openai_price.value(),'color_openai_quality':self.openai_quality.currentData(),
                'color_max_side':self.size.currentData(),
                **{'color_'+name+'_size':value for name,value in self._resolution_values.items()},
                'color_denoise_sigma':self.denoise.value(),
                'color_openai_interface':self.interface.currentData(),'color_network':self.network.currentData(),
                'color_timeout':self.timeout.value(),'color_square':self.square.isChecked(),
                'color_prompt':self.prompt.toPlainText().strip(),'color_history_pages':self.history.value()}
        values.update({'color_'+name:field.text().strip() for name,field in (self.local_fields|self.cloud_fields|self.openai_fields).items()})
        return values

    def save_key(self):
        set_color_api_key(self.key.text().strip(),self.persist.isChecked())
        set_openai_color_api_key(self.openai_key.text().strip(),self.openai_persist.isChecked())

    def set_testing(self,active):
        for button in (self.check,self.connection,self.models,self.image_test):button.setEnabled(not active)
        self.engine.setEnabled(not active);self.cancel_test.setVisible(active)

    def cancel_api(self):
        if self.task:
            self.task.cancel();self.status.setText('取消已请求，正在等待当前网络请求结束；已提交的图片请求仍可能计费。')

    def confirm_image_test(self):
        if self.task:return
        answer=QMessageBox.question(self,'图片上色测试可能收费','将向当前接口提交一张自制测试图（不上传书库漫画）。\n本次会调用图片模型，费用以服务商账单为准。是否开始？',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if answer==QMessageBox.StandardButton.Yes:self.start_api('image')

    def start_api(self,action):
        from .tasks import Task
        from .config import data_root,write_json
        from PIL import Image,ImageDraw
        if self.task:return
        settings=replace(self.settings,**self.values());engine=settings.color_engine
        key=(self.key if engine=='nano' else self.openai_key).text().strip()
        field=self.cloud_fields['nano_model'] if engine=='nano' else self.openai_fields['openai_model']
        started=time.time();model=field.text();self.set_testing(True);self.status.setText('正在'+{'models':'获取模型列表','connection':'验证连接与鉴权','image':'测试图片上色'}[action]+'…')
        def work(progress,cancel):
            api=None;record={'action':action,'engine':engine,'model':model,'started':started,'paid_request':action=='image'}
            try:
                api=ImageAPI(settings,engine,key,cancel)
                if action=='models':result={'models':api.get_models(),'message':'已获取模型列表'}
                elif action=='connection':result=api.test_connection()
                else:
                    image=Image.new('RGB',(1024,1024),'white');draw=ImageDraw.Draw(image)
                    draw.rectangle((10,10,150,230),outline='black',width=3);draw.ellipse((45,40,115,110),outline='black',width=3)
                    draw.rectangle((55,110,105,205),fill='#888888',outline='black',width=2)
                    output=api.colorize(image)
                    path=data_root()/'diagnostics/color-api'/f'test-{int(started*1000)}.png';path.parent.mkdir(parents=True,exist_ok=True);output.save(path)
                    if api.last_raw_result is not None:api.last_raw_result.save(path.with_stem(path.stem+'-model'))
                    record['geometry']=api.last_geometry
                    from .color_sizes import trace_text
                    record['dimensions']=api.last_sizes
                    result={'message':f'图片模型返回成功，接口：{api.last_interface}。\n测试图已保存：{path}\n此结果验证图片接口可用，真实漫画的质量与服务限制仍以实际页面为准。','preview':str(path)}
                    if api.last_geometry and api.last_geometry.get('warning'):result['message']+='\n'+api.last_geometry['warning']
                    result['message']+='\n'+trace_text(api.last_sizes)
                if cancel():raise InterruptedError('检测已取消；已提交的图片请求可能已计费')
                record.update(success=True,message=result.get('message',''));return result
            except Exception as error:
                record.update(success=False,message=sanitize(error,(key,)));raise
            finally:
                record['elapsed']=round(time.time()-started,3);record['finished']=time.time()
                if api:api.close()
                write_json(data_root()/'diagnostics/color-api'/f'{int(started*1000)}.json',record)
        self.task=Task(work,self)
        def success(result):
            if action=='models':
                models=result['models']
                if not models:self.status.setText('接口已响应，但服务商没有返回可用模型。');return
                dialog=ModelDialog(models,field.text(),self)
                if dialog.exec()==QDialog.DialogCode.Accepted:field.setText(dialog.selected_model());self.status.setText('已选择模型：'+field.text()+'；保存设置后生效。')
                else:self.status.setText(f'已获取 {len(models)} 个模型，未修改当前模型。')
            else:self.status.setText(result['message']+f'\n耗时：{time.time()-started:.2f} 秒')
        self.task.succeeded.connect(success)
        self.task.failed.connect(lambda error:self.status.setText(error+f'\n耗时：{time.time()-started:.2f} 秒'))
        self.task.paused.connect(self.status.setText)
        def finished():
            task=self.task;self.task=None;self.set_testing(False);task.deleteLater()
        self.task.finished.connect(finished);self.task.start()

    def check_local(self):
        from .tasks import Task
        from .color_backends import Klein
        if self.task:return
        settings=replace(self.settings,**self.values())
        def work(progress,cancel):
            backend=Klein(settings,None,None,cancel)
            try:backend.preflight();return 'Klein 连接正常，节点及三个模型文件均已找到'
            finally:backend.close()
        self.task=Task(work,self);self.set_testing(True);self.status.setText('正在检测…')
        self.task.succeeded.connect(self.status.setText);self.task.failed.connect(self.status.setText)
        def finished():
            task=self.task;self.task=None;self.set_testing(False);task.deleteLater()
        self.task.finished.connect(finished);self.task.start()


class ColorDialog(QDialog):
    def __init__(self,settings,count,current=None,edition='original',parent=None,*,library=None,book=None):
        super().__init__(parent);self.setWindowTitle('漫画上色 · 独立彩色版本');self.resize(640,750)
        outer=QVBoxLayout(self);scroll=QScrollArea();scroll.setWidgetResizable(True)
        content=QWidget();form=QFormLayout(content);scroll.setWidget(content);outer.addWidget(scroll)
        self.options=ColorOptions(settings)
        self.edition=QComboBox()
        for text,value in EDITIONS:self.edition.addItem(text,value)
        self.edition.setCurrentIndex(max(0,self.edition.findData(edition)));form.addRow('上色来源版本',self.edition)
        self.library,self.book=library,book
        self.dimensions=QLabel();self.dimensions.setWordWrap(True)
        self.dimensions.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow('页面尺寸',self.dimensions)
        form.addRow(self.options)
        self.scope=QComboBox();self.scope.addItems(['当前页','整本漫画','指定页面范围'])
        self.scope.model().item(0).setEnabled(current is not None);self.scope.setCurrentIndex(0 if current is not None else 1)
        self.start,self.end=QSpinBox(),QSpinBox()
        for field in (self.start,self.end):field.setRange(1,count)
        self.start.setValue((current or 0)+1);self.end.setValue(count)
        form.addRow('处理范围',self.scope);form.addRow('起始页',self.start);form.addRow('结束页',self.end)
        self.reference=QLineEdit();self.reference.setPlaceholderText('可选：官方彩页、封面或角色设定图')
        row=QWidget();layout=QHBoxLayout(row);layout.setContentsMargins(0,0,0,0);layout.addWidget(self.reference)
        choose=QPushButton('选择…');choose.clicked.connect(self.choose_reference);layout.addWidget(choose);form.addRow('配色参考图',row)
        self.force=QCheckBox('重新上色（另存新结果；云端会重新计费）');form.addRow(self.force)
        self.cost=QLabel();self.cost.setWordWrap(True);form.addRow(self.cost)
        self.error=QLabel();self.error.setWordWrap(True);form.addRow(self.error)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('开始上色')
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);outer.addWidget(buttons)
        self.current=current;self.count=count
        self.scope.currentIndexChanged.connect(self.update_scope)
        for signal in (self.start.valueChanged,self.end.valueChanged,self.options.price.valueChanged,self.options.openai_price.valueChanged,self.options.engine.currentIndexChanged):signal.connect(self.update_scope)
        self.update_scope()
        for signal in (self.edition.currentIndexChanged,self.scope.currentIndexChanged,self.start.valueChanged,self.options.size.currentIndexChanged,self.options.square.toggled,self.options.engine.currentIndexChanged,self.options.output_mode.currentIndexChanged,self.options.output_width.valueChanged,self.options.output_height.valueChanged):signal.connect(self.update_dimensions)
        self.update_dimensions()

    def update_dimensions(self,*_):
        if self.library is None:self.dimensions.setText('完成上色后，任务记录会显示来源、上传、模型返回和保存尺寸。');return
        from PIL import Image
        from .colorization import source_path,read_record
        from .color_sizes import size_text,trace_text,output_size,upload_dimensions
        indexes=self.indexes();index=indexes[0] if indexes else self.start.value()-1
        edition=self.edition.currentData()
        try:
            with Image.open(source_path(self.library,self.book,index,edition)) as image:
                source=image.size
            canvas,content=upload_dimensions(source,self.options.size.currentData(),self.options.square.isChecked() and self.options.engine.currentData()!='klein')
            text=f'第 {index+1} 页：来源 {size_text(source)}；预计上传 {size_text(canvas)}'
            try:
                requested=output_size(replace(self.options.settings,**self.options.values()),upload=canvas)
                text+='；请求 '+requested
                if 'x' in requested:
                    width,height=map(int,requested.split('x'))
                    if width*canvas[1]!=height*canvas[0]:text+='\n提示：自定义生成比例与上传比例不同，模型可能调整画面布局'
            except ValueError as error:text+='\n尺寸提示：'+str(error)
            saved=read_record(self.library,self.book,index,edition)
            if saved:
                trace=saved[0].get('dimensions')
                text+='\n最近上色：'+(trace_text(trace) if trace else '旧记录：模型 '+size_text((saved[0].get('geometry') or {}).get('model_size'))+'；上传尺寸未记录')
            self.dimensions.setText(text)
        except (OSError,ValueError,KeyError) as error:self.dimensions.setText(str(error))

    def choose_reference(self):
        path,_=QFileDialog.getOpenFileName(self,'选择配色参考图','','图片 (*.png *.jpg *.jpeg *.webp)')
        if path:self.reference.setText(path)

    def indexes(self):
        if self.scope.currentIndex()==0:return [self.current]
        if self.scope.currentIndex()==1:return list(range(self.count))
        return list(range(self.start.value()-1,self.end.value()))

    def update_scope(self,*args):
        for field in (self.start,self.end):field.setEnabled(self.scope.currentIndex()==2)
        count=len(self.indexes())
        if self.options.engine.currentData() in ('nano','openai'):
            price=self.options.openai_price.value() if self.options.engine.currentData()=='openai' else self.options.price.value()
            self.cost.setText(f'所选 {count} 页，按填写单价估算最多 ${count*price:.4f}；实际按请求计费。' if price else f'所选 {count} 页；未填写上色单价，暂无法估算费用。')
        else:self.cost.setText(f'所选 {count} 页；使用本地 Klein 4B，不产生云端上色费用。')

    def accept(self):
        from .color_backends import validate_url
        try:
            if not self.indexes():raise ValueError('起始页不能大于结束页')
            if self.options.task:raise ValueError('请等待连接检测完成')
            values=self.options.values()
            from .color_sizes import output_size
            output_size(replace(self.options.settings,**values))
            validate_url(values[{'klein':'color_comfy_url','nano':'color_nano_url','openai':'color_openai_url'}[values['color_engine']]])
            if values['color_engine']=='nano' and not self.options.key.text().strip():raise ValueError('请填写 Nano Banana 上色密钥')
            if values['color_engine']=='openai' and not self.options.openai_key.text().strip():raise ValueError('请填写 OpenAI 上色密钥')
            if values['color_engine']=='openai' and not values['color_openai_model']:raise ValueError('请填写 OpenAI 图像模型名称')
            if self.reference.text().strip() and not Path(self.reference.text().strip()).is_file():raise ValueError('参考图片不存在')
            self.options.save_key()
        except Exception as error:self.error.setText(str(error));return
        super().accept()

    def reject(self):
        if self.options.task:
            self.error.setText('正在检测连接，请等待检测完成后关闭');return
        super().reject()
