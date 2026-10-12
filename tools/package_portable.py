"""Attach documentation, cached OCR models and license notices."""
import argparse
import importlib.metadata as metadata
import shutil
from pathlib import Path
from assemble_standalone import copy_asset

ROOT=Path(__file__).resolve().parent.parent
parser=argparse.ArgumentParser()
parser.add_argument("--models",action="store_true")
args=parser.parse_args()
destination=ROOT/"dist"/"阅译"
if not (destination/"阅译.exe").is_file():
    raise SystemExit("Build the application first.")
for filename in ("icuuc.dll","icudt78.dll"):
    (destination/"_internal"/filename).unlink(missing_ok=True)
# The Japanese tokenizer needs its dictionary files in addition to Python code.
import unidic_lite
shutil.copytree(Path(unidic_lite.__file__).parent,destination/"_internal"/"unidic_lite",dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__"))
for source,target in [("README.md","使用说明.md"),("THIRD_PARTY.md","第三方组件.md"),("requirements-tested.txt","requirements-tested.txt"),
                      ('漫画上色说明.md','漫画上色说明.md')]:
    shutil.copy2(ROOT/source,destination/target)
licenses=destination/"licenses"
licenses.mkdir(exist_ok=True)
for name in ("PySide6","torch","torchvision","manga-ocr","rapidocr","onnxruntime","transformers","huggingface-hub",
             "opencv-python","numpy","Pillow","httpx","beautifulsoup4","EbookLib","keyring","PyInstaller"):
    distribution=metadata.distribution(name)
    for relative in distribution.files or []:
        if not any(part in str(relative).lower() for part in ("license","copying","notice")):continue
        source=Path(distribution.locate_file(relative))
        if not source.is_file():continue
        parent=licenses/name
        parent.mkdir(exist_ok=True)
        # Keep disambiguating package directories for license files with the same basename.
        filename="_".join(Path(str(relative).replace("\\","/")).parts[-3:])
        shutil.copy2(source,parent/filename)
if args.models:
    shutil.copytree(ROOT/"data"/"models",destination/"data"/"models",dirs_exist_ok=True,copy_function=copy_asset,
                    ignore=shutil.ignore_patterns(".locks","*.lock"))
if not (ROOT/'engines/manga/manifest.json').is_file():
    raise SystemExit('Restore the owned engines from the installed reader before building.')
from assemble_standalone import assemble
assemble(destination)
if (ROOT/"assets"/"licenses").exists():
    shutil.copytree(ROOT/"assets"/"licenses",licenses,dirs_exist_ok=True)
size=sum(file.stat().st_size for file in destination.rglob("*") if file.is_file())
print(f"Portable folder ready ({size/1024**3:.2f} GiB): {destination}")
