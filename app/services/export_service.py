"""导出服务（《项目文档》FR-10 / §13.11、《技术文档》§3.3 ``exports/``）。

* Markdown 导出：单文件，含课程名与日期（AC-46），图片 / SVG 保持相对路径（AC-29）；
* PDF 导出：Markdown → HTML → 交给**注入的打印器**。
  服务层不导入 Qt（§1.5 红线 2），QtWebEngine 的打印实现在 ``app/ui/export_helper.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..infra import file_repository
from . import lesson_service

EXPORT_SCOPES: tuple[tuple[str, str], ...] = (
    ("notes", "仅笔记"),
    ("script", "仅讲义"),
    ("all", "讲义 + 追问 + 笔记"),
)


@dataclass
class ExportResult:
    path: Path
    scope: str
    size_bytes: int = 0
    lessons: list[str] = field(default_factory=list)


def export_dir(course_dir: str | Path) -> Path:
    return Path(course_dir) / "exports"


def _lesson_dirs(course_dir: Path, lesson_ids: list[str] | None) -> list[Path]:
    dirs = lesson_service.iter_lesson_dirs(course_dir)
    if not lesson_ids:
        return dirs
    wanted = set(lesson_ids)
    from . import notes_service

    return [item for item in dirs if notes_service.lesson_id_of(item) in wanted]


def collect_markdown(
    course_dir: str | Path,
    *,
    scope: str = "all",
    lesson_ids: list[str] | None = None,
) -> tuple[str, list[str]]:
    """组装单文件 Markdown（返回文本与涉及的小节 id）。"""
    course = Path(course_dir)
    meta = file_repository.read_json_or_default(course / "course.json", {}) or {}
    outline = file_repository.read_json_or_default(course / "outline.json", {}) or {}

    lines = [
        f"# {meta.get('name', course.name)}",
        "",
        f"> 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}｜范围："
        + dict(EXPORT_SCOPES).get(scope, scope),
    ]
    if meta.get("description"):
        lines += ["", str(meta["description"])]
    if outline.get("learning_goals"):
        lines += ["", "## 学习目标", ""]
        lines += [f"- {goal}" for goal in outline["learning_goals"]]

    touched: list[str] = []
    for chapter in outline.get("chapters", []):
        chapter_lines: list[str] = []
        for lesson_spec in chapter.get("lessons", []):
            lesson_dir = next(
                (item for item in _lesson_dirs(course, [str(lesson_spec.get("id"))])), None
            )
            if lesson_dir is None:
                continue
            content = lesson_service.load_lesson(lesson_dir)
            touched.append(content.lesson_id)
            chapter_lines += ["", f"### {content.lesson_id} {content.title}", ""]
            if scope in ("script", "all"):
                chapter_lines.append(content.markdown.strip() or "（本节还没有讲义）")
                chapter_lines.append("")
            if scope == "all":
                records = lesson_service.load_chat(lesson_dir)
                if records:
                    chapter_lines += ["#### 追问记录", ""]
                    for record in records:
                        role = "我" if record.get("role") == "user" else "老师"
                        text = str(record.get("text") or record.get("md") or "").strip()
                        chapter_lines.append(f"- **{role}**：{text[:500]}")
                    chapter_lines.append("")
            if scope in ("notes", "all"):
                from . import notes_service as _notes

                note_file = _notes.note_path_for(course, lesson_dir)
                if note_file.exists():
                    chapter_lines += ["#### 笔记", "", note_file.read_text(encoding="utf-8").strip(), ""]
        if chapter_lines:
            lines += ["", f"## {chapter.get('id', '')} {chapter.get('title', '')}"]
            lines += chapter_lines
    return "\n".join(lines).rstrip() + "\n", touched


def export_markdown(
    course_dir: str | Path,
    *,
    scope: str = "all",
    lesson_ids: list[str] | None = None,
    target: str | Path | None = None,
) -> ExportResult:
    """导出 Markdown（文件名含课程名与日期，AC-46）。"""
    course = Path(course_dir)
    meta = file_repository.read_json_or_default(course / "course.json", {}) or {}
    name = str(meta.get("name") or course.name)
    text, lessons = collect_markdown(course, scope=scope, lesson_ids=lesson_ids)
    suffix = {"notes": "笔记", "script": "讲义", "all": "合集"}.get(scope, scope)
    destination = (
        Path(target)
        if target is not None
        else export_dir(course) / f"{name}-{suffix}-{datetime.now().strftime('%Y%m%d')}.md"
    )
    file_repository.write_text(destination, text)
    return ExportResult(path=destination, scope=scope, size_bytes=len(text.encode("utf-8")), lessons=lessons)


def render_html(markdown_text: str, *, title: str = "AI Teaches Me 导出") -> str:
    """Markdown → HTML（离线：只用本地依赖，公式沿用 KaTeX 的 $$ 约定）。"""
    try:
        import markdown as markdown_module

        body = markdown_module.markdown(
            markdown_text,
            extensions=["tables", "fenced_code", "toc", "sane_lists"],
        )
    except Exception:  # noqa: BLE001 - 没有 python-Markdown 时退化为纯文本包裹
        body = "<pre>" + markdown_text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\" />"
        f"<title>{title}</title>"
        "<style>body{font-family:'Microsoft YaHei',sans-serif;line-height:1.75;margin:32px auto;max-width:820px;}"
        "table{border-collapse:collapse;}td,th{border:1px solid #ccc;padding:4px 8px;}"
        "pre{background:#f5f5f5;padding:12px;overflow-x:auto;}</style></head><body>"
        + body
        + "</body></html>"
    )


def export_pdf(
    course_dir: str | Path,
    *,
    printer,
    scope: str = "all",
    lesson_ids: list[str] | None = None,
    target: str | Path | None = None,
) -> ExportResult:
    """导出 PDF：调用注入的 ``printer(html, path) -> bool``（QtWebEngine 在 UI 层）。"""
    course = Path(course_dir)
    meta = file_repository.read_json_or_default(course / "course.json", {}) or {}
    name = str(meta.get("name") or course.name)
    text, lessons = collect_markdown(course, scope=scope, lesson_ids=lesson_ids)
    html = render_html(text, title=name)
    destination = (
        Path(target)
        if target is not None
        else export_dir(course) / f"{name}-{datetime.now().strftime('%Y%m%d')}.pdf"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not printer(html, destination):
        raise RuntimeError("PDF 打印失败（渲染内核不可用）")
    size = destination.stat().st_size if destination.exists() else 0
    return ExportResult(path=destination, scope=scope, size_bytes=size, lessons=lessons)
