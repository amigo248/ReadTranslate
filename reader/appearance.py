"""Readable application colors and explicit checkbox indicators."""
import re,sys
from pathlib import Path
from PySide6.QtGui import QColor,QPalette

DEFAULT_BACKGROUND='#101722'
PRESETS=(('深色',DEFAULT_BACKGROUND),('浅色','#f4f6f8'),('纸张色','#f6f1e7'),('深蓝','#15273c'),('自定义',None))

def normalize_color(value):
    if not isinstance(value,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',value.strip()):
        raise ValueError('请输入六位颜色代码，例如 #101722，或点击“选择颜色”')
    return value.strip().lower()

def luminance(color):
    c=QColor(color)
    def channel(n):
        v=n/255
        return v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4
    return .2126*channel(c.red())+.7152*channel(c.green())+.0722*channel(c.blue())

def contrast(a,b):
    light,dark=sorted((luminance(a),luminance(b)),reverse=True)
    return (light+.05)/(dark+.05)

def blend(base,target,fraction):
    a,b=QColor(base),QColor(target)
    return QColor(*(round(x+(y-x)*fraction) for x,y in zip(a.getRgb()[:3],b.getRgb()[:3]))).name()

def readable(base,foreground,minimum,others=()):
    for amount in range(101):
        color=blend(base,foreground,amount/100)
        if all(contrast(color,b)>=minimum for b in (base,*others)):return color
    return foreground

def colors(value):
    try:background=normalize_color(value)
    except ValueError:background=DEFAULT_BACKGROUND
    foreground=max(('#000000','#ffffff'),key=lambda c:contrast(c,background))
    dark=foreground=='#ffffff'
    def surface(amount):
        value=blend(background,foreground,amount)
        if contrast(value,foreground)<4.5:
            value=blend(background,'#000000' if dark else '#ffffff',amount)
        return value
    control,raised,hover=surface(.06),surface(.10),surface(.16)
    return {'background':background,'foreground':foreground,'dark':dark,
            'surface':control,'raised':raised,
            'hover':hover,'muted':readable(background,foreground,4.5,(control,raised,hover)),
            'border':readable(background,foreground,3),'check':'#087f70'}

BASE_STYLE="""
QWidget { background:#101722; color:#dce5ef; font-family:'Microsoft YaHei UI'; font-size:14px; }
QMainWindow { background:#101722; }
QWidget#sidebar { background:#0b111b; border-right:1px solid #243042; }
QLabel#brand { font-size:25px; font-weight:700; color:#edf8f5; }
QLabel#heading { font-size:28px; font-weight:700; }
QLabel#muted { color:#8495ab; }
QLabel#readerTitle { font-size:16px; font-weight:600; }
QPushButton { background:#1b2737; border:1px solid #2b3d53; border-radius:7px; padding:9px 15px; }
QPushButton:hover { background:#29394d; border-color:#4b667d; }
QPushButton:pressed { background:#152131; }
QPushButton:disabled { color:#637080; }
QPushButton#primary { color:#081c17; background:#56d6b5; border-color:#56d6b5; font-weight:700; }
QPushButton#primary:hover { background:#7be5c9; }
QPushButton#nav { text-align:left; padding:13px 20px; border:0; background:transparent; }
QPushButton#nav:checked { background:#17392f; color:#7ce4c9; }
QLineEdit,QPlainTextEdit,QTextBrowser,QSpinBox,QDoubleSpinBox,QComboBox {
 background:#0e1520; border:1px solid #2b3a4d; border-radius:6px; padding:7px; selection-background-color:#285c50;
}
QComboBox { min-height:23px; }
QComboBox QAbstractItemView { background:#182334; selection-background-color:#285c50; }
QListWidget { background:transparent; border:0; outline:0; }
QListWidget::item { background:#192434; border:1px solid #2b394d; border-radius:10px; padding:12px; margin:7px; }
QListWidget::item:selected { background:#203b38; border:1px solid #59d9b7; }
QListWidget::item:hover { border-color:#597085; }
QTableWidget { background:#111a27; border:1px solid #28384c; gridline-color:#253449; }
QHeaderView::section { background:#1c293b; border:0; padding:10px; color:#93a6bb; }
QProgressBar { border:1px solid #2c3e51; border-radius:5px; text-align:center; height:18px; }
QProgressBar::chunk { background:#48bfa1; border-radius:4px; }
QScrollBar:vertical { background:#111a26; width:10px; }
QScrollBar::handle:vertical { background:#3c4e65; border-radius:5px; min-height:30px; }
QScrollBar::add-line,QScrollBar::sub-line { height:0; }
QMenu { background:#172333; border:1px solid #3b5064; padding:5px; }
QMenu::item { padding:9px 22px; }
QMenu::item:selected { background:#285c50; }
QGroupBox { border:1px solid #2b3b50; border-radius:8px; margin-top:13px; padding-top:13px; }
QGroupBox::title { subcontrol-origin:margin; left:12px; }
QStatusBar { background:#0c1420; color:#889bb0; }
QToolTip { background:#213247; color:#edf4fa; border:1px solid #4a657a; }
"""

def stylesheet(value=DEFAULT_BACKGROUND):
    p=colors(value);base=p['background'];fg=p['foreground'];style=BASE_STYLE
    if base!=DEFAULT_BACKGROUND:
        replacement={'#101722':base,'#0b111b':p['surface'],'#dce5ef':fg,'#edf8f5':fg,
            '#8495ab':p['muted'],'#1b2737':p['raised'],'#29394d':p['hover'],
            '#152131':p['surface'],'#637080':p['muted'],'#0e1520':p['surface'],
            '#182334':p['raised'],'#192434':p['surface'],'#111a27':p['surface'],
            '#1c293b':p['raised'],'#93a6bb':p['muted'],'#111a26':p['surface'],
            '#3c4e65':p['border'],'#172333':p['raised'],'#0c1420':p['surface'],
            '#889bb0':p['muted'],'#213247':p['raised'],'#edf4fa':fg,
            '#17392f':'#174336' if p['dark'] else '#c5eddf',
            '#7ce4c9':'#a2ffe5' if p['dark'] else '#074635',
            '#203b38':'#174336' if p['dark'] else '#c5eddf','#285c50':'#087f70'}
        for color in ('#243042','#2b3d53','#4b667d','#2b3a4d','#2b394d','#597085',
                      '#28384c','#253449','#2c3e51','#3b5064','#2b3b50','#4a657a'):
            replacement[color]=p['border']
        style=re.sub(r'#[0-9a-fA-F]{6}',lambda m:replacement.get(m.group(),m.group()),style)
    style+='\nQWidget:disabled { color:'+p['muted']+'; }\n'
    style+='QLineEdit,QPlainTextEdit,QTextBrowser,QComboBox { selection-color:#ffffff; }\n'
    style+='QComboBox QAbstractItemView { selection-color:#ffffff; } QMenu::item:selected {color:#ffffff;}\n'
    assets=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))/'assets'
    checked=(assets/'check-mark.svg').as_posix();partial=(assets/'check-partial.svg').as_posix()
    indicators=('QCheckBox::indicator','QGroupBox::indicator','QAbstractItemView::indicator','QMenu::indicator')
    def rule(state,body):return ','.join(item+state for item in indicators)+' {'+body+'}\n'
    style+='QCheckBox {spacing:8px; padding:2px; border:1px solid transparent;}\n'
    style+='QCheckBox:focus {border:1px solid '+p['border']+';border-radius:3px;}\n'
    style+=rule('',f'width:18px;height:18px;border:2px solid {p["border"]};border-radius:3px;background:{base};')
    style+=rule(':unchecked','image:none;')
    style+=rule(':checked',f'background:{p["check"]};border-color:{p["check"]};image:url("{checked}");')
    style+=rule(':indeterminate',f'background:{p["check"]};border-color:{p["check"]};image:url("{partial}");')
    style+=rule(':disabled',f'border-color:{p["muted"]};')
    style+=rule(':checked:disabled','background:#65717e;border-color:#65717e;')
    style+=rule(':indeterminate:disabled','background:#65717e;border-color:#65717e;')
    return style

def apply_appearance(application,settings):
    if application is None:return
    values=colors(settings.background_color);style=stylesheet(values['background'])
    if application.styleSheet()==style and application.palette().color(QPalette.ColorRole.Window).name()==values['background']:return
    palette=QPalette(application.palette())
    for role,key in ((QPalette.ColorRole.Window,'background'),(QPalette.ColorRole.WindowText,'foreground'),
                     (QPalette.ColorRole.Base,'surface'),(QPalette.ColorRole.AlternateBase,'raised'),
                     (QPalette.ColorRole.Button,'raised'),(QPalette.ColorRole.ButtonText,'foreground'),
                     (QPalette.ColorRole.Text,'foreground'),(QPalette.ColorRole.PlaceholderText,'muted'),
                     (QPalette.ColorRole.ToolTipBase,'raised'),(QPalette.ColorRole.ToolTipText,'foreground'),
                     (QPalette.ColorRole.Mid,'border')):
        palette.setColor(role,QColor(values[key]))
    palette.setColor(QPalette.ColorRole.Highlight,QColor('#087f70'))
    palette.setColor(QPalette.ColorRole.HighlightedText,QColor('#ffffff'))
    for role in (QPalette.ColorRole.WindowText,QPalette.ColorRole.ButtonText,QPalette.ColorRole.Text):
        palette.setColor(QPalette.ColorGroup.Disabled,role,QColor(values['muted']))
    application.setPalette(palette)
    application.setStyleSheet(style)
    for widget in application.allWidgets():widget.update()

STYLE=stylesheet()

from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,QLineEdit,QPushButton,QCheckBox,QColorDialog

class AppearanceOptions(QWidget):
    """Preview local to this panel; only saving settings changes the application."""
    def __init__(self,settings,parent=None):
        super().__init__(parent);layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        layout.addWidget(QLabel('界面背景颜色'))
        row=QHBoxLayout();self.preset=QComboBox()
        for text,value in PRESETS:self.preset.addItem(text,value)
        self.color=QLineEdit(colors(settings.background_color)['background'])
        self.color.setPlaceholderText('#101722');self.color.setMaximumWidth(115)
        self.color.setAccessibleName('界面背景颜色代码')
        choose=QPushButton('选择颜色');reset=QPushButton('恢复默认')
        choose.clicked.connect(self.choose_color);reset.clicked.connect(lambda:self.color.setText(DEFAULT_BACKGROUND))
        row.addWidget(self.preset,1);row.addWidget(self.color);row.addWidget(choose);row.addWidget(reset);layout.addLayout(row)
        self.preview=QWidget();preview=QVBoxLayout(self.preview)
        preview.addWidget(QLabel('效果预览'))
        for text,checked,enabled in [('未勾选',False,True),('已勾选',True,True),('不可用的选项',True,False)]:
            box=QCheckBox(text);box.setChecked(checked);box.setEnabled(enabled);preview.addWidget(box)
        layout.addWidget(self.preview)
        self.message=QLabel();self.message.setWordWrap(True);layout.addWidget(self.message)
        self.preset.currentIndexChanged.connect(self.choose_preset)
        self.color.textChanged.connect(self.refresh_preview);self.refresh_preview()
    def choose_preset(self):
        value=self.preset.currentData()
        if value:self.color.setText(value)
    def choose_color(self):
        color=QColorDialog.getColor(QColor(colors(self.color.text())['background']),self,'选择界面背景颜色',QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if color.isValid():self.color.setText(color.name())
    def value(self):return normalize_color(self.color.text())
    def refresh_preview(self):
        try:value=self.value()
        except ValueError as error:self.message.setText(str(error));return
        self.preview.setStyleSheet(stylesheet(value))
        self.preset.blockSignals(True);index=self.preset.findData(value)
        self.preset.setCurrentIndex(index if index>=0 else len(PRESETS)-1);self.preset.blockSignals(False)
        self.message.setText('保存后立即生效，文字和控件颜色会自动适配。')
