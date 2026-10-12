from __future__ import annotations

import html
import json
import mimetypes
import shutil
import tempfile
import zipfile
from pathlib import Path

from .library import Library
from .pipeline import edit_path, translated_path
from .text_replace import suppressed_paragraphs


def chapter_html(unit: dict, translations: list[str] | None, resource_prefix="", suppressed=()) -> str:
    parts = []
    images = unit.get("images", [])

    def append_images(after):
        for image in images:
            if image["after"] == after:
                parts.append(f'<p class="illustration"><img src="{html.escape(resource_prefix + image["path"], quote=True)}" alt="插图" /></p>')

    append_images(-1)
    for index, text in enumerate(unit["paragraphs"]):
        if translations is None:
            parts.append('<p class="original">' + html.escape(text).replace("\n", "<br />") + "</p>")
        if translations is not None and index not in suppressed:
            translated = translations[index] if index < len(translations) else ""
            parts.append('<p class="translation">' + html.escape(translated or "〔此段尚未翻译〕").replace("\n", "<br />") + "</p>")
        append_images(index)
    return "\n".join(parts)


def export_book(library: Library, book_id: str, destination: Path, format: str,
                indexes=None, progress=lambda *_: None, cancel=lambda: False,edition='translated'):
    manifest = library.manifest(book_id)
    record = library.book(book_id)
    title = record["title"]
    root = library.path(book_id)
    indexes = list(range(len(manifest["units"]))) if indexes is None else indexes
    if not indexes:
        raise ValueError("没有可导出的内容")
    destination = destination.resolve()
    if destination.is_relative_to(root.resolve()):
        raise ValueError("请将译本导出到书库原版目录之外")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if manifest["kind"] == "comic":
        files = []
        for index in indexes:
            path = translated_path(library, book_id, index)
            if edition.startswith('colored_'):
                from .colorization import available_color
                path=available_color(library,book_id,index,edition.removeprefix('colored_'))
                if path is None:raise ValueError(f'第 {index+1} 页没有有效的对应彩色版本，请先手动上色')
            elif edition in ('upscaled_original','upscaled_translated'):
                from .upscaling import available_edition
                path=available_edition(library,book_id,index,'original' if edition=='upscaled_original' else 'translated')
                if path is None:raise ValueError(f'第 {index+1} 页没有有效的超分结果，请先生成超分版本')
            elif edition!='translated':raise ValueError('导出版本无效')
            if not path.exists():
                raise ValueError(f"第 {index+1} 页尚未生成译图；请先翻译或校对排版")
            files.append(path)
        if format == "folder":
            if destination.exists():
                raise ValueError("目标文件夹已存在，请选择新的目录名称")
            temporary = Path(tempfile.mkdtemp(prefix=".read-translate-", dir=destination.parent))
            try:
                for position, path in enumerate(files):
                    if cancel():
                        raise InterruptedError("导出已暂停")
                    shutil.copy2(path, temporary / f"{indexes[position]+1:05d}.png")
                    progress(position+1,len(files),"导出漫画译图")
                (temporary / "作品信息.json").write_text(json.dumps({"title": title, "source": manifest.get("source", ""), "pages": indexes},ensure_ascii=False,indent=2),encoding="utf-8")
                temporary.rename(destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        else:
            temporary = destination.with_suffix(destination.suffix + ".part")
            try:
                with zipfile.ZipFile(temporary,"w",zipfile.ZIP_STORED) as archive:
                    for position,path in enumerate(files):
                        if cancel():
                            raise InterruptedError("导出已暂停")
                        archive.write(path, f"{indexes[position]+1:05d}.png")
                        progress(position+1,len(files),"打包漫画译本")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
    else:
        units = []
        for index in indexes:
            unit = manifest["units"][index]
            path = edit_path(library,book_id,index)
            edits = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            translations = edits.get("translations", [])
            suppressed = suppressed_paragraphs(edits)
            if len(translations) != len(unit["paragraphs"]) or any(not text.strip() and i not in suppressed for i, text in enumerate(translations)):
                raise ValueError(f"章节「{unit['title']}」仍有未翻译段落，请先完成翻译或校对")
            unit=dict(unit)
            unit["original_title"]=unit["title"]
            unit["title"]=edits.get('title_translation','') if edits.get('global_replacement_title',{}).get('base') else (edits.get('title_translation') or unit['title'])
            units.append((unit, translations, suppressed))
        if format == "txt":
            text = [title + "（中文译本）"]
            for unit, translations, suppressed in units:
                if cancel():
                    raise InterruptedError("导出已暂停")
                text.append("\n" + unit["title"])
                for index, translated in enumerate(translations):
                    if index not in suppressed:
                        text.append(translated)
            temporary = destination.with_suffix(destination.suffix + ".part")
            temporary.write_text("\n\n".join(text),encoding="utf-8-sig")
            temporary.replace(destination)
        else:
            from ebooklib import epub
            book = epub.EpubBook()
            book.set_identifier(book_id + "-zh")
            book.set_title(title + "（中文译本）")
            book.set_language("zh")
            if record["author"]:
                book.add_author(record["author"])
            added_images = set()
            chapters = []
            css = epub.EpubItem(uid="style",file_name="style.css",media_type="text/css",
                               content=b"body{line-height:1.8} p{text-indent:2em} .original{color:#777} img{max-width:100%;height:auto}")
            book.add_item(css)
            for position, (unit, translations, suppressed) in enumerate(units):
                if cancel():
                    raise InterruptedError("导出已暂停")
                for image in unit.get("images",[]):
                    relative = image["path"]
                    if relative not in added_images:
                        book.add_item(epub.EpubItem(uid=f"image-{len(added_images)}",file_name=relative,
                                                    media_type=mimetypes.guess_type(relative)[0] or "image/png",content=(root / relative).read_bytes()))
                        added_images.add(relative)
                chapter = epub.EpubHtml(title=unit["title"],file_name=f"chapter-{position+1}.xhtml",lang="zh")
                chapter.content = f"<h1>{html.escape(unit['title'])}</h1>" + chapter_html(unit, translations, suppressed=suppressed)
                chapter.add_item(css)
                book.add_item(chapter)
                chapters.append(chapter)
                progress(position+1,len(units),"生成小说译本")
            book.toc = chapters
            book.spine = ["nav",*chapters]
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())
            temporary = destination.with_suffix(".part.epub")
            try:
                epub.write_epub(str(temporary),book)
                with zipfile.ZipFile(temporary) as archive:
                    if archive.read("mimetype") != b"application/epub+zip" or archive.testzip():
                        raise ValueError("导出的电子书验证失败")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
    return str(destination)


def backup_library(library: Library, destination: Path, progress=lambda *_: None, cancel=lambda: False):
    temporary = destination.with_suffix(".part.zip")
    with tempfile.TemporaryDirectory(dir=library.root) as staging:
        import sqlite3
        snapshot = Path(staging) / "library.sqlite3"
        with library.connect() as source:
            target=sqlite3.connect(snapshot)
            try:
                source.backup(target)
            finally:
                target.close()
        files = [file for file in library.books.rglob("*") if file.is_file()]
        try:
            with zipfile.ZipFile(temporary,"w",zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot,"library.sqlite3")
                for index,file in enumerate(files):
                    if cancel():
                        raise InterruptedError("备份已暂停")
                    archive.write(file,str(file.relative_to(library.root)))
                    progress(index+1,len(files),"备份书库")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return str(destination)
