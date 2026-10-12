from __future__ import annotations

import hashlib
import io
import posixpath
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote

from PIL import Image

from .library import Library, digest_file, natural_key

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
BOOK_SUFFIXES = IMAGE_SUFFIXES | {".cbz", ".zip", ".epub", ".txt", ".html", ".htm"}
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_IMAGE_BYTES = 100 * 1024**2


def validate_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 15000 or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError("压缩包过大：最多 15000 个文件，解压内容不超过 2GB")
        if any(entry.flag_bits & 1 for entry in entries):
            raise ValueError("不支持加密压缩包")


def image_dimensions(path: Path):
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        return image.size


def decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "cp932", "gb18030", "big5"):
        try:
            text = raw.decode(encoding)
            if "\x00" not in text:
                return text
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError("无法识别文本编码，请转换为 UTF-8 后导入")


def text_chapters(text: str, title: str) -> list[dict]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    heading = re.compile(r"^\s*(?:第\s*[\d一二三四五六七八九十百千零〇]+\s*[章話话回節节卷]|Chapter\s+\d+|プロローグ|エピローグ).{0,65}$", re.I)
    chapters = []
    lines = []
    name = title

    def append():
        content = "\n".join(lines).strip()
        if not content:
            return
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", content) if part.strip()]
        if len(paragraphs) == 1:
            paragraphs = [line.strip() for line in content.splitlines() if line.strip()]
        chapters.append({"title": name, "paragraphs": paragraphs, "images": []})

    for line in text.splitlines():
        if heading.match(line):
            append()
            name = line.strip()
            lines = []
        else:
            lines.append(line)
    append()
    return chapters


def html_content(html: str, image_resolver=None) -> dict:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(["script", "style", "nav", "header", "footer", "rt", "rp"]):
        node.decompose()
    container = soup.body or soup
    paragraphs, images = [], []
    block_tags = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre"]
    for node in container.find_all(block_tags + ["img"]):
        if node.name == "img":
            if image_resolver:
                path = image_resolver(node.get("src", ""))
                if path:
                    images.append({"path": path, "after": len(paragraphs) - 1})
            continue
        if node.find(block_tags):
            continue
        text = node.get_text("", strip=False).strip()
        if text:
            paragraphs.append(text)
    if not paragraphs:
        paragraphs = [line.strip() for line in container.get_text("\n").splitlines() if line.strip()]
    return {"paragraphs": paragraphs, "images": images}


def import_epub(path: Path, target: Path) -> tuple[str, str, list[dict], str]:
    from ebooklib import ITEM_IMAGE, epub
    validate_archive(path)
    with zipfile.ZipFile(path) as archive:
        if "META-INF/encryption.xml" in archive.namelist():
            raise ValueError("这份 EPUB 含加密资源；请导入无加密的 EPUB 或 TXT")
    book = epub.read_epub(str(path), options={"ignore_ncx": True})
    title = (book.get_metadata("DC", "title") or [(path.stem, {})])[0][0]
    author = (book.get_metadata("DC", "creator") or [("", {})])[0][0]
    assets = {}
    cover = ""
    (target / "assets").mkdir(exist_ok=True)
    for item in book.get_items_of_type(ITEM_IMAGE):
        suffix = Path(item.file_name).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            continue
        relative = "assets/" + hashlib.sha256(item.file_name.encode()).hexdigest()[:20] + suffix
        (target / relative).write_bytes(item.get_content())
        assets[posixpath.normpath(unquote(item.file_name))] = relative
        if not cover or "cover" in item.file_name.casefold():
            cover = relative
    chapters = []
    for item_id, _linear in book.spine:
        item = book.get_item_with_id(item_id)
        if not item or item_id in {"nav", "ncx"}:
            continue
        parent = posixpath.dirname(item.file_name)

        def resolve(src):
            return assets.get(posixpath.normpath(posixpath.join(parent, unquote(src.split("#")[0]))))

        html = item.get_content().decode("utf-8", errors="replace")
        content = html_content(html, resolve)
        if content["paragraphs"] or content["images"]:
            content["title"] = content["paragraphs"][0][:60] if content["paragraphs"] else f"插图 {len(chapters) + 1}"
            chapters.append(content)
    if not chapters:
        raise ValueError("电子书中没有可读取章节")
    return title, author, chapters, cover


def import_book(library: Library, path: Path, progress=lambda *_: None, cancel=lambda: False) -> str:
    path = path.resolve()
    image_paths = []
    if path.is_dir():
        image_paths = sorted((file for file in path.rglob("*") if file.is_file() and file.suffix.lower() in IMAGE_SUFFIXES), key=natural_key)
        if not image_paths:
            raise ValueError("文件夹中没有支持的漫画图片")
        digest = hashlib.sha256()
        for file in image_paths:
            if cancel():
                raise InterruptedError("导入已暂停")
            digest.update(str(file.relative_to(path)).encode())
            digest.update(digest_file(file).encode())
        fingerprint = digest.hexdigest()
    else:
        if path.suffix.lower() not in BOOK_SUFFIXES:
            raise ValueError("支持图片、CBZ/ZIP、TXT、HTML 和 EPUB")
        fingerprint = digest_file(path)
    duplicate = library.duplicate(fingerprint)
    if duplicate:
        progress(1, 1, "作品已经在书库中")
        return duplicate
    book_id = uuid.uuid4().hex
    target = library.path(book_id)
    target.mkdir(parents=True)
    manifest = {"id": book_id, "title": path.stem if path.is_file() else path.name,
                "author": "", "source": str(path), "kind": "comic", "units": [], "cover": ""}
    try:
        suffix = path.suffix.lower()
        if not path.is_dir():
            (target / "original").mkdir()
            shutil.copy2(path, target / "original" / path.name)
        if path.is_dir() or suffix in IMAGE_SUFFIXES:
            (target / "pages").mkdir()
            for index, file in enumerate(image_paths or [path]):
                if cancel():
                    raise InterruptedError("导入已暂停")
                width, height = image_dimensions(file)
                relative = f"pages/{index:06d}{file.suffix.lower()}"
                shutil.copy2(file, target / relative)
                chapter = str(file.parent.relative_to(path)) if path.is_dir() else manifest["title"]
                manifest["units"].append({"title": file.stem, "chapter": chapter, "path": relative, "width": width, "height": height})
                progress(index + 1, len(image_paths or [path]), f"导入 {file.name}")
        elif suffix in {".cbz", ".zip"}:
            validate_archive(path)
            (target / "pages").mkdir()
            with zipfile.ZipFile(path) as archive:
                entries = sorted((item for item in archive.infolist() if not item.is_dir() and Path(item.filename).suffix.lower() in IMAGE_SUFFIXES), key=lambda item: natural_key(item.filename))
                if not entries:
                    raise ValueError("压缩包中没有漫画图片")
                for index, entry in enumerate(entries):
                    if cancel():
                        raise InterruptedError("导入已暂停")
                    if entry.file_size > MAX_IMAGE_BYTES:
                        raise ValueError("单张图片超过 100MB")
                    relative = f"pages/{index:06d}{Path(entry.filename).suffix.lower()}"
                    # Never extract external filenames: archive traversal cannot escape the book directory.
                    with archive.open(entry) as source, (target / relative).open("wb") as dest:
                        shutil.copyfileobj(source, dest)
                    width, height = image_dimensions(target / relative)
                    manifest["units"].append({"title": Path(entry.filename).stem,
                                              "chapter": str(Path(entry.filename).parent), "path": relative,
                                              "width": width, "height": height})
                    progress(index + 1, len(entries), f"导入 {entry.filename}")
        elif suffix == ".epub":
            title, author, chapters, cover = import_epub(path, target)
            manifest.update(title=title, author=author, kind="novel", units=chapters, cover=cover)
        else:
            text = decode_text(path.read_bytes())
            if suffix in {".html", ".htm"}:
                chapters = [{"title": manifest["title"], **html_content(text)}]
            else:
                chapters = text_chapters(text, manifest["title"])
            if not chapters or not any(chapter["paragraphs"] for chapter in chapters):
                raise ValueError("文件中没有小说正文")
            manifest.update(kind="novel", units=chapters)
        if manifest["kind"] == "comic":
            manifest["cover"] = manifest["units"][0]["path"]
        library.add(manifest, fingerprint)
        progress(len(manifest["units"]), len(manifest["units"]), "导入完成")
        return book_id
    except Exception:
        # Only remove the new UUID directory created by this operation.
        if target.parent == library.books and target.name == book_id:
            shutil.rmtree(target, ignore_errors=True)
        raise
