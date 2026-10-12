# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

data=collect_data_files("rapidocr")+collect_data_files("manga_ocr")+collect_data_files("unidic_lite")+[("assets/icon.ico","assets"),("assets/check-mark.svg","assets"),("assets/check-partial.svg","assets"),("reader/mtu_worker.py",".")]
data += [("assets/color-denoise","assets/color-denoise")]
analysis=Analysis(["main.py"],pathex=[],binaries=[],datas=data,
    hiddenimports=["reader.ocr_worker","keyring.backends.Windows"],hookspath=[],
    excludes=["tkinter","PyQt5","PyQt6","PySide2","tensorflow","keras","jax","jaxlib",
              "torchaudio","matplotlib","pandas","sklearn","IPython","pytest","notebook"],
    noarchive=False)
# Qt uses Windows' native ICU. A Poppler tool on the build machine's PATH has
# a same-named, incompatible ICU DLL; do not ship that unrelated dependency.
analysis.binaries=[entry for entry in analysis.binaries if entry[0].lower() not in {"icuuc.dll","icudt78.dll"}]
pyz=PYZ(analysis.pure)
gui=EXE(pyz,analysis.scripts,[],exclude_binaries=True,name="阅译",console=False,
        debug=False,strip=False,upx=False,icon="assets/icon.ico")
ocr=EXE(pyz,analysis.scripts,[],exclude_binaries=True,name="阅译OCR",console=True,
        debug=False,strip=False,upx=False,icon="assets/icon.ico")
collection=COLLECT(gui,ocr,analysis.binaries,analysis.datas,strip=False,upx=False,name="阅译")
