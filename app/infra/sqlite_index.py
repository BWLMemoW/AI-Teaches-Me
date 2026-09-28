"""SQLite 索引库（《技术文档》§3.7；可整库删除后重建的缓存层）。

要点：

* 文件系统是唯一事实来源，本库只负责「查得快」（§2.2）；
* 中文检索一律 ``tokenize='trigram'``，长度 < 3 的查询回退 ``LIKE``（§3.7 定稿结论 C-03）；
* 标签与智能视图除写库外还落盘 ``Courses/.library/``，删库不丢（§3.7 可重建性规则 2）；
* 写操作串行化（单写多读），避免并发写锁冲突。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS courses (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
  updated_at TEXT, total_lessons INTEGER, completed_lessons INTEGER,
  cover TEXT, description TEXT
);

CREATE TABLE IF NOT EXISTS materials (
  id TEXT PRIMARY KEY, course_id TEXT, filename TEXT, kind TEXT,
  pages INTEGER, parsed_at TEXT, status TEXT, checksum TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS material_chunks USING fts5(
  material_id UNINDEXED, page UNINDEXED, heading, text,
  tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS usage_log (
  ts TEXT, course_id TEXT, lesson_id TEXT, agent TEXT,
  model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER
);

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  color TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS course_tags (
  course_id TEXT NOT NULL, tag_id INTEGER NOT NULL,
  PRIMARY KEY (course_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_course_tags_tag ON course_tags(tag_id);

CREATE TABLE IF NOT EXISTS personas (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, builtin INTEGER DEFAULT 0,
  path TEXT, version INTEGER DEFAULT 1, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS qa_sessions (
  id TEXT PRIMARY KEY, course_id TEXT, title TEXT,
  created_at TEXT, updated_at TEXT, message_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS saved_views (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  query TEXT NOT NULL, created_at TEXT
);
"""

TRIGRAM_MIN_LENGTH = 3


def now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


class SqliteIndex:
    """索引库门面。线程安全（内部锁 + 单写多读）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.initialize()

    # --- 生命周期 -------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteIndex":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def initialize(self) -> None:
        with self.transaction() as conn:
            conn.executescript(DDL)

    def is_trigram_supported(self) -> bool:
        with self._lock:
            try:
                self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _probe USING fts5(x, tokenize='trigram')")
                self._conn.execute("DROP TABLE _probe")
                return True
            except sqlite3.OperationalError:
                return False

    # --- 课程索引 -------------------------------------------------------
    def upsert_course(
        self,
        *,
        course_id: str,
        name: str,
        path: str,
        updated_at: str | None = None,
        total_lessons: int = 0,
        completed_lessons: int = 0,
        cover: str | None = None,
        description: str = "",
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO courses (id, name, path, updated_at, total_lessons, completed_lessons, cover, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name, path=excluded.path, updated_at=excluded.updated_at,
                  total_lessons=excluded.total_lessons, completed_lessons=excluded.completed_lessons,
                  cover=excluded.cover, description=excluded.description
                """,
                (course_id, name, path, updated_at or now_iso(), total_lessons, completed_lessons, cover, description),
            )

    def delete_course(self, course_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM course_tags WHERE course_id = ?", (course_id,))
            conn.execute("DELETE FROM materials WHERE course_id = ?", (course_id,))
            conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))

    def list_courses(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM courses ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def rebuild_from_courses(self, courses: Iterable[Any]) -> int:
        """按扫描结果重建课程索引（课程删除后残留记录会被清掉）。"""
        seen: list[str] = []
        for summary in courses:
            self.upsert_course(
                course_id=summary.id,
                name=summary.name,
                path=str(summary.path),
                total_lessons=summary.lessons,
                completed_lessons=summary.completed_lessons,
                description=summary.description,
            )
            seen.append(summary.id)
        with self.transaction() as conn:
            if seen:
                placeholders = ",".join("?" for _ in seen)
                conn.execute(f"DELETE FROM courses WHERE id NOT IN ({placeholders})", seen)
            else:
                conn.execute("DELETE FROM courses")
        return len(seen)

    # --- 标签 -----------------------------------------------------------
    def add_tag(self, name: str, color: str | None = None) -> int:
        with self.transaction() as conn:
            row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
            if row is not None:
                return int(row["id"])
            cursor = conn.execute(
                "INSERT INTO tags (name, color, created_at) VALUES (?, ?, ?)",
                (name, color, now_iso()),
            )
            return int(cursor.lastrowid or 0)

    def rename_tag(self, tag_id: int, name: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id))

    def merge_tags(self, source_id: int, target_id: int) -> None:
        with self.transaction() as conn:
            rows = conn.execute("SELECT course_id FROM course_tags WHERE tag_id = ?", (source_id,)).fetchall()
            for row in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO course_tags (course_id, tag_id) VALUES (?, ?)",
                    (row["course_id"], target_id),
                )
            conn.execute("DELETE FROM course_tags WHERE tag_id = ?", (source_id,))
            conn.execute("DELETE FROM tags WHERE id = ?", (source_id,))

    def delete_tag(self, tag_id: int) -> None:
        """删除标签只解除关联，不动课程数据（AC-54）。"""
        with self.transaction() as conn:
            conn.execute("DELETE FROM course_tags WHERE tag_id = ?", (tag_id,))
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))

    def prune_unused_tags(self) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM course_tags)"
            )
            return int(cursor.rowcount or 0)

    def list_tags(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.*, COUNT(ct.course_id) AS course_count
                FROM tags t LEFT JOIN course_tags ct ON ct.tag_id = t.id
                GROUP BY t.id ORDER BY t.name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_course_tags(self, course_id: str, tag_ids: Sequence[int]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM course_tags WHERE course_id = ?", (course_id,))
            for tag_id in tag_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO course_tags (course_id, tag_id) VALUES (?, ?)",
                    (course_id, int(tag_id)),
                )

    def tags_for_course(self, course_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.* FROM tags t
                JOIN course_tags ct ON ct.tag_id = t.id
                WHERE ct.course_id = ? ORDER BY t.name
                """,
                (course_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def courses_with_all_tags(self, tag_ids: Sequence[int]) -> list[str]:
        """交集筛选（AC-42）。"""
        ids = [int(item) for item in tag_ids]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT course_id FROM course_tags WHERE tag_id IN ({placeholders})
                GROUP BY course_id HAVING COUNT(DISTINCT tag_id) = ?
                """,
                (*ids, len(ids)),
            ).fetchall()
        return [str(row["course_id"]) for row in rows]

    def courses_with_any_tags(self, tag_ids: Sequence[int]) -> list[str]:
        """并集筛选（AC-42）。"""
        ids = [int(item) for item in tag_ids]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT DISTINCT course_id FROM course_tags WHERE tag_id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
        return [str(row["course_id"]) for row in rows]

    # --- 智能视图 -------------------------------------------------------
    def save_view(self, name: str, query: dict[str, Any]) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO saved_views (name, query, created_at) VALUES (?, ?, ?)",
                (name, json.dumps(query, ensure_ascii=False), now_iso()),
            )
            return int(cursor.lastrowid or 0)

    def list_views(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM saved_views ORDER BY name").fetchall()
        views = []
        for row in rows:
            item = dict(row)
            try:
                item["query"] = json.loads(item["query"])
            except (TypeError, ValueError):
                item["query"] = {}
            views.append(item)
        return views

    def delete_view(self, view_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))

    # --- 资料与检索（M5 使用） ------------------------------------------
    def upsert_material(
        self,
        *,
        material_id: str,
        course_id: str,
        filename: str,
        kind: str,
        pages: int = 0,
        status: str = "parsed",
        checksum: str = "",
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO materials (id, course_id, filename, kind, pages, parsed_at, status, checksum)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  course_id=excluded.course_id, filename=excluded.filename, kind=excluded.kind,
                  pages=excluded.pages, parsed_at=excluded.parsed_at, status=excluded.status,
                  checksum=excluded.checksum
                """,
                (material_id, course_id, filename, kind, pages, now_iso(), status, checksum),
            )

    def index_chunks(self, material_id: str, chunks: Sequence[dict[str, Any]]) -> int:
        with self.transaction() as conn:
            conn.execute("DELETE FROM material_chunks WHERE material_id = ?", (material_id,))
            for chunk in chunks:
                conn.execute(
                    "INSERT INTO material_chunks (material_id, page, heading, text) VALUES (?, ?, ?, ?)",
                    (
                        material_id,
                        int(chunk.get("page") or 0),
                        str(chunk.get("heading") or ""),
                        str(chunk.get("text") or ""),
                    ),
                )
        return len(chunks)

    def search_chunks(self, query: str, *, k: int = 5, material_ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """中文检索：trigram 走索引；查询串 < 3 字回退 LIKE（§3.7）。"""
        text = (query or "").strip()
        if not text:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if len(text) >= TRIGRAM_MIN_LENGTH:
            clauses.append("material_chunks MATCH ?")
            params.append('"' + text.replace('"', '""') + '"')
        else:
            clauses.append("text LIKE ?")
            params.append(f"%{text}%")
        if material_ids:
            placeholders = ",".join("?" for _ in material_ids)
            clauses.append(f"material_id IN ({placeholders})")
            params.extend(material_ids)
        where = " AND ".join(clauses)
        sql = f"SELECT material_id, page, heading, text FROM material_chunks WHERE {where} LIMIT ?"
        params.append(int(k))
        with self._lock:
            try:
                rows = self._conn.execute(sql, tuple(params)).fetchall()
            except sqlite3.OperationalError:
                rows = self._conn.execute(
                    "SELECT material_id, page, heading, text FROM material_chunks WHERE text LIKE ? LIMIT ?",
                    (f"%{text}%", int(k)),
                ).fetchall()
        return [dict(row) for row in rows]

    # --- 用量与人设 -----------------------------------------------------
    def log_usage(
        self,
        *,
        ts: str,
        course_id: str | None,
        lesson_id: str | None,
        agent: str | None,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO usage_log (ts, course_id, lesson_id, agent, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, course_id, lesson_id, agent, model, prompt_tokens, completion_tokens),
            )

    def usage_summary(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                FROM usage_log
                """
            ).fetchone()
        return {
            "calls": int(row["calls"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
        }

    def upsert_persona(self, *, persona_id: str, name: str, builtin: bool = False, path: str = "", version: int = 1) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO personas (id, name, builtin, path, version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name, builtin=excluded.builtin, path=excluded.path,
                  version=excluded.version, updated_at=excluded.updated_at
                """,
                (persona_id, name, 1 if builtin else 0, path, int(version), now_iso()),
            )

    def list_personas(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM personas ORDER BY builtin DESC, name").fetchall()
        return [dict(row) for row in rows]

    def upsert_qa_session(self, *, session_id: str, course_id: str, title: str, message_count: int = 0) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO qa_sessions (id, course_id, title, created_at, updated_at, message_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  title=excluded.title, updated_at=excluded.updated_at, message_count=excluded.message_count
                """,
                (session_id, course_id, title, now_iso(), now_iso(), int(message_count)),
            )

    def list_qa_sessions(self, course_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM qa_sessions WHERE course_id = ? ORDER BY updated_at DESC", (course_id,)
            ).fetchall()
        return [dict(row) for row in rows]
