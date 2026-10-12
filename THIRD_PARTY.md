# 第三方组件

阅译的窗口、识别与文件处理使用以下组件；组件代码和模型遵循各自的许可。对应版本见 `requirements-tested.txt`，打包工具为 PyInstaller。

| 组件 | 上游项目 |
| --- | --- |
| PySide6 / Qt | https://doc.qt.io/qtforpython-6/licenses.html |
| PyTorch / torchvision | https://github.com/pytorch/pytorch / https://github.com/pytorch/vision |
| Manga OCR / 日文识别模型 | https://github.com/kha-white/manga-ocr / https://huggingface.co/kha-white/manga-ocr-base |
| RapidOCR | https://github.com/RapidAI/RapidOCR |
| ONNX Runtime | https://github.com/microsoft/onnxruntime |
| Transformers / Hugging Face Hub | https://github.com/huggingface/transformers / https://github.com/huggingface/huggingface_hub |
| OpenCV / NumPy / Pillow | https://github.com/opencv/opencv / https://github.com/numpy/numpy / https://github.com/python-pillow/Pillow |
| HTTPX / Beautiful Soup / EbookLib / keyring | https://github.com/encode/httpx / https://www.crummy.com/software/BeautifulSoup/ / https://github.com/aerkalov/ebooklib / https://github.com/jaraco/keyring |
| PyInstaller | https://pyinstaller.org/en/stable/license.html |

便携版保留动态 Qt 库与第三方组件的许可文件。阅译源代码、构建脚本与应用逻辑在项目目录中提供；源码不包含导入的漫画、小说及其生成结果。

## 内置漫画翻译组件

- 超分引擎 Real-ESRGAN NCNN Vulkan：https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan；Windows 组件采用 MTU 指定的 20220424 包，原始包与模型来源及 SHA-256 保存在 engines/manga/models/upscaling/sources.json。
- Real-CUGAN：https://github.com/bilibili/ailab/tree/main/Real-CUGAN；独立携带 2、3、4 倍 conservative 模型，使用已内置的 MTU PyTorch 实现。
- MangaJaNai / IllustrationJaNai：https://github.com/the-database/MangaJaNai；独立携带 2、4 倍 1600p 黑白漫画模型及 2、4 倍彩图模型，来自 MTU 指定的已校验发布文件。3 倍通过 4 倍输出缩回。各模型使用上游各自的许可，MTU 的源码许可不替代模型许可。

- MTU 翻译核心，GPL-3.0，源码及许可在 `engines/manga`；上游 https://github.com/hgmzhn/manga-translator-ui，版本 f0307a063214f915f2b1d6e5cd3233f3bf78339f。
- 独立 Python 3.12、PyTorch、PyQt6、CUDA 和相关依赖的许可文件保留在内置 Python 的各软件包目录。检测/48px OCR/MangaLens/LaMa 模型和字体是本机已安装文件的未修改副本；项目的许可不替代各模型/字体的许可。
- mocr 使用 `kha-white/manga-ocr-base`（模型标注 Apache-2.0），六个权重/配置文件独立存入 `engines/manga/models/ocr/manga_ocr`，来自阅译既有的本地模型缓存。模型说明保留在 `licenses/MangaOCR-model-card.md`；不依赖原 MTU 或用户全局 Hugging Face 缓存。
- 图像处理引擎的逐文件哈希及来源保留在各自 manifest.json。大型运行库、模型及字体不包含在 GitHub 源码目录中；便携程序保留使用的组件许可，分发时还应保留对应上游源码和许可说明。

- FFDNet RGB 上色输入降噪：Matias Tassano，2018，GPL-3.0-or-later。独立模型及许可、参考源码保存在 assets/color-denoise（安装版为 _internal/assets/color-denoise），权重来源和 SHA-256 见 source.json。参考 MTU 的本地上色预处理并扩展至三个上色引擎。

## 公开发布版字体与源码

公开便携版使用 Noto Sans CJK SC Regular/Bold（SIL OFL 1.1），字体文件与许可在 engines/manga/fonts，固定文件来源和 SHA-256 见 assets/release-fonts.json。公开包不复制开发电脑的 Windows 字体及旧字体库。

当前漫画引擎对应源码、配置和 GPL-3.0 许可收录在 vendor/manga。NumPy 2.2.6 的 numpy/_core/tests/_natype.py 会被 numpy.testing 在 SciPy 初始化期间使用，属于运行依赖，应保留，不能按测试目录名称删除。
