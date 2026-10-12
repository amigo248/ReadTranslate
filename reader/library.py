from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import data_root, write_json


def natural_key(value: object) -> list:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value))]


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:100]
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", cleaned):
        cleaned = "_" + cleaned
    return cleaned or "未命名"


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Library:
    def __init__(self, root: Path | None = None):
        self.root = root or data_root()
        self.books = self.root / "books"
        self.books.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "library.sqlite3"
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
                    author TEXT DEFAULT '', tags TEXT DEFAULT '', source TEXT DEFAULT '',
                    fingerprint TEXT UNIQUE, progress INTEGER DEFAULT 0,
                    scroll REAL DEFAULT 0, favorite INTEGER DEFAULT 0,
                    created REAL, opened REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT,
                    kind TEXT, status TEXT, done INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
                    message TEXT DEFAULT '', payload TEXT DEFAULT '{}', updated REAL,
                    created REAL, finished REAL
                );
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT, model TEXT,
                    input_tokens INTEGER, output_tokens INTEGER, cost REAL, created REAL
                );
                CREATE TABLE IF NOT EXISTS recycled_books (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, recycled REAL
                );
            """)
            columns={row['name'] for row in conn.execute('PRAGMA table_info(jobs)')}
            if 'created' not in columns:conn.execute('ALTER TABLE jobs ADD COLUMN created REAL')
            if 'finished' not in columns:
                conn.execute('ALTER TABLE jobs ADD COLUMN finished REAL')
                # The historical update on completion/failure is a known end time;
                # an original start time was never stored and must not be invented.
                conn.execute("UPDATE jobs SET finished=updated WHERE status IN ('complete','failed')")
            conn.execute("UPDATE jobs SET status='paused', message='程序已关闭，可继续任务' WHERE status='running'")
        self.restore_recycled_books()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_books(self, query="", kind="all", favorite=False):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM books WHERE (title LIKE ? OR author LIKE ? OR tags LIKE ?) "
                "AND (?='all' OR kind=?) AND (?=0 OR favorite=1) ORDER BY opened DESC, created DESC",
                (f"%{query}%", f"%{query}%", f"%{query}%", kind, kind, int(favorite)))]

    def book(self, book_id: str):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            return dict(row) if row else None

    def duplicate(self, fingerprint: str):
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM books WHERE fingerprint=?", (fingerprint,)).fetchone()
            return row[0] if row else None

    def add(self, manifest: dict, fingerprint: str) -> None:
        self.save_manifest(manifest["id"], manifest)
        with self.connect() as conn:
            conn.execute("INSERT INTO books(id,title,kind,author,source,fingerprint,created) VALUES(?,?,?,?,?,?,?)",
                         (manifest["id"], manifest["title"], manifest["kind"], manifest.get("author", ""),
                          manifest.get("source", ""), fingerprint, time.time()))

    def path(self, book_id: str) -> Path:
        # IDs are internal UUIDs; never accept external paths as book IDs.
        if not re.fullmatch(r"[a-f0-9]{32}", book_id):
            raise ValueError("无效作品编号")
        return self.books / book_id

    def manifest(self, book_id: str) -> dict:
        return json.loads((self.path(book_id) / "manifest.json").read_text(encoding="utf-8"))

    def save_manifest(self, book_id: str, manifest: dict):
        write_json(self.path(book_id) / "manifest.json", manifest)

    def update(self, book_id: str, **values):
        allowed = {"title", "author", "tags", "progress", "scroll", "favorite", "opened", "source"}
        if not values or set(values) - allowed:
            raise ValueError("无效书库字段")
        with self.connect() as conn:
            conn.execute("UPDATE books SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                         (*values.values(), book_id))

    def checked_book_folder(self, book_id: str) -> Path:
        folder = self.path(book_id)
        books = self.books.resolve()
        if not books.is_relative_to(self.root.resolve()) or folder.is_symlink() or folder.resolve().parent != books:
            raise ValueError("作品目录超出书库范围，已停止操作")
        return folder

    @staticmethod
    def _move_to_trash(folder: Path) -> bool:
        from PySide6.QtCore import QFile
        if not QFile.supportsMoveToTrash():
            return False
        return QFile(folder.resolve().as_posix()).moveToTrash()

    def recycle_book(self, book_id: str) -> None:
        folder = self.checked_book_folder(book_id)
        if not folder.is_dir():
            raise FileNotFoundError("作品目录不存在，无法移入回收站")
        # Commit recovery information before calling the OS. If interrupted before
        # the move, startup restores the still-present folder and its records.
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            if record is None:
                raise ValueError("作品已不在书库中")
            jobs = [dict(row) for row in conn.execute("SELECT * FROM jobs WHERE book_id=?", (book_id,))]
            if any(job["status"] == "running" for job in jobs):
                raise ValueError("作品有正在进行的任务，请先暂停任务")
            payload = json.dumps({"book": dict(record), "jobs": jobs}, ensure_ascii=False)
            conn.execute("INSERT OR REPLACE INTO recycled_books VALUES(?,?,?)", (book_id, payload, time.time()))
            conn.execute("DELETE FROM jobs WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM books WHERE id=?", (book_id,))
        try:
            if not self._move_to_trash(folder):
                raise OSError("无法移入回收站，作品文件已保留。请检查磁盘回收站设置或文件是否被其他程序占用。")
        except Exception:
            self.restore_recycled_books()
            raise

    def restore_recycled_books(self) -> list[str]:
        restored = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT * FROM recycled_books").fetchall():
                try:
                    folder = self.checked_book_folder(row["id"])
                    if not folder.is_dir():
                        continue
                    manifest = self.manifest(row["id"])
                    payload = json.loads(row["payload"])
                    record = payload["book"]
                    if manifest["id"] != row["id"] or record["id"] != row["id"]:
                        continue
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if conn.execute("SELECT 1 FROM books WHERE id=?", (row["id"],)).fetchone():
                    continue
                # A fresh import may already use the fingerprint; keep both books
                # so restoring an older translation never overwrites the new one.
                if conn.execute("SELECT 1 FROM books WHERE fingerprint=?", (record["fingerprint"],)).fetchone():
                    record["fingerprint"] = None
                columns = ("id", "title", "kind", "author", "tags", "source", "fingerprint", "progress",
                           "scroll", "favorite", "created", "opened")
                conn.execute("INSERT INTO books(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                             tuple(record[key] for key in columns))
                job_columns = ("id", "book_id", "kind", "status", "done", "total", "message", "payload", "updated", "created", "finished")
                for job in payload.get("jobs", []):
                    if job["book_id"] != row["id"]:
                        continue
                    if job["status"] == "running":
                        job["status"] = "paused"
                    job.setdefault('finished',job.get('updated') if job['status'] in {'complete','failed'} else None)
                    conn.execute("INSERT OR IGNORE INTO jobs(" + ",".join(job_columns) + ") VALUES(" +
                                 ",".join("?" for _ in job_columns) + ")", tuple(job.get(key) for key in job_columns))
                conn.execute("DELETE FROM recycled_books WHERE id=?", (row["id"],))
                restored.append(row["id"])
        return restored

    def create_job(self, book_id, kind, total, payload=None):
        now=time.time()
        with self.connect() as conn:
            cursor = conn.execute("INSERT INTO jobs(book_id,kind,status,total,payload,updated,created) VALUES(?,?,'running',?,?,?,?)",
                                  (book_id, kind, total, json.dumps(payload or {}, ensure_ascii=False), now,now))
            return cursor.lastrowid

    def update_job(self, job_id, done, status="running", message=""):
        now=time.time();finished=now if status in {'complete','failed'} else None
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET done=?,status=?,message=?,updated=?,finished=? WHERE id=?",
                         (done, status, message, now,finished,job_id))

    def jobs(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT jobs.*, books.title FROM jobs LEFT JOIN books ON jobs.book_id=books.id ORDER BY updated DESC,jobs.id DESC")]

    @staticmethod
    def record_ids(ids):
        ids=list(dict.fromkeys(ids))
        if any(type(value) is not int or value<1 for value in ids):raise ValueError('无效记录编号')
        return ids

    def delete_jobs(self,ids):
        ids=self.record_ids(ids)
        if not ids:return 0
        slots=','.join('?' for _ in ids)
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute(f"SELECT 1 FROM jobs WHERE id IN ({slots}) AND status='running'",ids).fetchone():
                raise ValueError('所选任务仍在进行中，请先暂停并等待保存完成后再删除记录。')
            return conn.execute(f'DELETE FROM jobs WHERE id IN ({slots})',ids).rowcount

    def record_usage(self, book_id, model, input_tokens, output_tokens, cost):
        with self.connect() as conn:
            conn.execute("INSERT INTO usage(book_id,model,input_tokens,output_tokens,cost,created) VALUES(?,?,?,?,?,?)",
                         (book_id, model, input_tokens, output_tokens, cost, time.time()))

    def usage(self):
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) requests, COALESCE(SUM(input_tokens),0) input_tokens, "
                               "COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(cost),0) cost FROM usage").fetchone()
            return dict(row)

    def usage_records(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT usage.*,books.title FROM usage LEFT JOIN books '
                'ON usage.book_id=books.id ORDER BY usage.created DESC,usage.id DESC')]

    def delete_usage_records(self,ids):
        ids=self.record_ids(ids)
        if not ids:return 0
        with self.connect() as conn:
            return conn.execute('DELETE FROM usage WHERE id IN ('+','.join('?' for _ in ids)+')',ids).rowcount
