# 阅译

Windows 桌面漫画与小说书库，支持本地阅读、日译中、文字校对、漫画超分和上色。

## 功能

- 导入图片、图片文件夹、CBZ/ZIP 漫画和 TXT/EPUB/HTML 小说；导入时复制源文件。
- 漫画单页、双页、整本连续预览、滚轮翻页、自动阅读和无边框窗口；保存阅读进度。
- 原版／中文版、超分／普通、彩色／黑白三个开关组合为八种阅读版本。
- 云端文字翻译，漫画图片不发送给文字翻译接口；翻译失败可继续任务。
- 两种漫画处理流程：RapidOCR + Manga OCR，以及内置漫画检测、48px／mocr、LaMa 和 Qt 排版。
- 鼠标框选识别和翻译、逐框修改文字／字体／字号／粗细／横竖排，以及手动扩大译文范围。
- 中文在气泡与分镜安全区域内换行、换列和缩小字号；仍放不下时在校对界面标红，继续处理其他文字。
- 局部重绘、清除原字残迹、查找替换和保存全局替换规则。四种中文版分别校对，只更新当前版本；原版不提供校对。
- Real-ESRGAN、Real-CUGAN、MangaJaNai 超分，支持仅超分与先超分后翻译。
- Klein／ComfyUI、Gemini 和 OpenAI Images 上色；支持连接测试、获取模型、图片测试、具体报错和上色降噪。上色仅处理图片，不执行检测、OCR、翻译和文字渲染。
- 请求生成尺寸可选 auto（精确跟随实际上传画布）或自定义；上传补白和比例还原见[漫画上色说明](漫画上色说明.md)。
- 漫画导出 PNG／CBZ、小说导出 TXT／EPUB，作品删除与回收站恢复、任务时间及用量记录管理。

## 运行便携版

下载 Windows 发布包，按 [发布说明](发布说明.md) 中的步骤双击安装脚本解压，再运行其中的 `启动阅译.cmd` 或 `阅译.exe`。本机已有构建也可通过根目录的 `启动阅译.cmd` 运行 `dist/阅译/阅译.exe`。移动程序时复制完整的 `dist/阅译` 文件夹，保留 `_internal`、`阅译OCR.exe`、`engines` 和 `data/models`。本地阅读、识别、修复、排版和超分无需原 MTU 环境；云端翻译、云端上色需要有效接口，本地 Klein 需要已启动的 ComfyUI。

首次打开书库为空；在“我的书库”导入作品。设置中的 API 地址、模型和密码框由用户填写；勾选保存后，密钥写入 Windows 凭据管理器，不写入书库配置。费用由服务商实际账单决定。

## 源码运行与构建

采用 PySide6、SQLite、独立 OCR 子进程和后台任务。建议使用 Python 3.13，固定依赖版本见 `requirements-tested.txt`。

源码不包含大型运行库、模型与字体。完整图像处理或重新打包前，先解压同版本完整便携包，再运行 `tools/restore_portable_assets.py <解压后的阅译目录>`，校验并恢复 `engines/manga` 与 `data/models`；这些目录已被 Git 忽略。漫画引擎对应源码和修改说明位于 `vendor/manga`。

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
 # 补齐独立引擎、模型和字体（替换为你的便携版路径）
.\.venv\Scripts\python.exe tools/restore_portable_assets.py D:\Apps\阅译
# 需要 NVIDIA GPU 时安装匹配运行库
powershell -ExecutionPolicy Bypass -File .\install-gpu.ps1
.\.venv\Scripts\python.exe main.py
# 完整构建
python -m pip --python .\.venv\Scripts\python.exe install pyinstaller
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

只更新阅读程序 Python 代码时，可用 `tools/rebuild_reader_modules.py <完整便携版目录> <暂存目录>`；要求相同 Python 字节码版本。工具验证入口、阅读模块和第三方代码，再关闭阅译后替换两套 EXE；修改 `reader/mtu_worker.py` 时同时替换 `_internal/mtu_worker.py`。

## 数据与发布

书库默认在程序旁的 `data`，可通过 `READ_TRANSLATE_DATA` 设置位置。`data/books` 保存导入副本和生成结果，`library.sqlite3` 保存作品／任务／用量，`settings.json` 保存选项。源码版和便携版默认使用不同书库；同一书库只打开一个程序实例。

`.gitignore` 排除书库、设置、数据库、缓存、日志、生成图片所在目录、开发临时目录、模型和运行库。GitHub 提交源代码、资源和必要构建脚本；便携程序另行打包，先移除个人设置、书库、历史记录及缓存，并保留所需模型和许可。公开发布包由 `tools/prepare_release.py` 从指定完整运行目录生成，使用全新的设置，不复制本机配置。源码包和普通用户的便携版下载方式见 [发布说明](发布说明.md)。

程序启动错误查看 `data/application.log`；任务错误和 API 返回原因也会显示在界面。第三方组件与模型来源见 [THIRD_PARTY.md](THIRD_PARTY.md)，其许可与模型／字体的许可分别适用。

源码采用 GPL-3.0；完整许可在 `LICENSE`。公开便携版使用 Noto Sans CJK 常规／粗体，携带 SIL OFL 1.1 许可，可覆盖中文及日文。原有本机安装的字体选择保留。
