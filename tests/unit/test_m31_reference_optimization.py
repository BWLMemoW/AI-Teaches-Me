"""M31 测试：指定参考的三项优化。

* **A 逐页要点**：全貌用摘要覆盖整份课件，长课本也只占预算，且是等距抽样而不是只看前几页；
* **B 按页取原文**：``read_material_slice`` 真能取到某一页，检索限定在本课程 / 本节指定资料内；
* **C 课件覆盖自检**：知识点讲全之外，还要检查讲义有没有漏掉课件的主题。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.agents.teacher import TeacherAgent
from app.agents.tools import build_registry
from app.core.paths import atomic_write_json
from app.infra.sqlite_index import SqliteIndex
from app.services.lesson_service import generate_script
from app.services.material_service import (
    build_page_digest,
    material_page_digest,
    resolve_reference_digest,
)


def _make_course(root: Path, *, pages: int = 200, name: str = "课件课") -> Path:
    course_dir = root / name
    lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一节"
    lesson_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(course_dir / "course.json", {"id": "c-ref", "name": name})
    atomic_write_json(
        course_dir / "outline.json",
        {
            "schema_version": 1,
            "course_id": "c-ref",
            "chapters": [
                {
                    "id": "C1",
                    "title": "第一章",
                    "lessons": [
                        {
                            "id": "L1-1",
                            "title": "第一节",
                            "source_refs": ["m-book"],
                            "knowledge_points": [],
                        }
                    ],
                }
            ],
        },
    )
    atomic_write_json(lesson_dir / "lesson.json", {"id": "L1-1", "title": "第一节", "status": "not_started"})
    parsed = course_dir / "materials" / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        course_dir / "materials" / "materials.json",
        {
            "materials": [
                {
                    "id": "m-book",
                    "filename": "课本.pdf",
                    "kind": "pdf",
                    "pages": pages,
                    "status": "parsed",
                    "checksum": "c1",
                }
            ]
        },
    )
    chunks = [
        {
            "page": index,
            "heading": f"第{index}节主题" if index % 25 == 0 else "",
            "text": f"第 {index} 页正文内容。" * 8,
            "source": "text",
            "locator": f"#p{index}",
        }
        for index in range(1, pages + 1)
    ]
    atomic_write_json(parsed / "课本.chunks.json", {"version": 1, "chunks": chunks})
    return course_dir


# --- A 逐页要点 -------------------------------------------------------------
def test_build_page_digest_groups_by_page() -> None:
    digest = build_page_digest(
        [
            {"page": 1, "heading": "封面", "text": "第一页正文" * 3, "locator": "#s1"},
            {"page": 1, "heading": "", "text": "续写内容", "locator": "#s1"},
            {"page": 2, "heading": "", "text": "第二页", "locator": "#s2"},
        ],
        gist_chars=40,
    )
    assert [item["page"] for item in digest] == [1, 2]
    assert digest[0]["heading"] == "封面"
    assert "第一页正文" in digest[0]["gist"]
    assert len(digest[0]["gist"]) <= 41
    assert digest[1]["heading"] == ""


def test_material_page_digest_is_cached(tmp_path: Path) -> None:
    course_dir = _make_course(tmp_path, pages=3)
    entry = {"id": "m-book", "filename": "课本.pdf", "kind": "pdf", "checksum": "c1"}
    first = material_page_digest(course_dir, entry)
    cache = course_dir / "materials" / "parsed" / "课本.digest.json"
    assert cache.exists()
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["checksum"] == "c1" and payload["version"] == 1
    assert len(first) == 3

    # checksum 变了 → 旧缓存作废，按新内容重算
    atomic_write_json(
        course_dir / "materials" / "parsed" / "课本.chunks.json",
        {"version": 1, "chunks": [{"page": 1, "heading": "", "text": "全新内容", "locator": "#p1"}]},
    )
    second = material_page_digest(course_dir, {**entry, "checksum": "c2"})
    assert len(second) == 1 and "全新内容" in second[0]["gist"]


def test_long_material_is_sampled_not_truncated(tmp_path: Path) -> None:
    """回归：以前只喂前 3600 字，一本 200 页的课本后 150 页全丢。"""
    course_dir = _make_course(tmp_path, pages=200)
    result = resolve_reference_digest(course_dir, ["m-book"], budget_chars=1200)
    assert result["total_items"] == 200
    assert result["truncated"] is True
    pages = [item["page"] for item in result["items"]]
    assert pages[0] == 1
    assert max(pages) >= 150  # 后段照样有代表，而不是只截前面
    assert len(result["items"]) < 200  # 但仍然守预算


def test_short_material_keeps_every_page(tmp_path: Path) -> None:
    course_dir = _make_course(tmp_path, pages=8)
    result = resolve_reference_digest(course_dir, ["m-book"], budget_chars=6000)
    assert result["truncated"] is False
    assert [item["page"] for item in result["items"]] == list(range(1, 9))


# --- B 按页取原文 -----------------------------------------------------------
def test_index_can_fetch_a_single_page(tmp_path: Path) -> None:
    index = SqliteIndex(tmp_path / "data" / "library.db")
    try:
        index.upsert_material(
            material_id="m-x", course_id="c-ref", filename="a.pptx", kind="pptx", pages=5, status="parsed", checksum=""
        )
        index.index_chunks(
            "m-x",
            [
                {"page": 3, "heading": "核心", "text": "第三页原文", "source": "text"},
                {"page": 4, "heading": "", "text": "第四页原文", "source": "text"},
            ],
        )
        rows = index.chunks_for_page("m-x", 3)
        assert len(rows) == 1 and "第三页原文" in rows[0]["text"]
        assert index.chunks_for_page("m-x", 99) == []
    finally:
        index.close()


def test_read_material_slice_tool_is_real_and_scoped(tmp_path: Path) -> None:
    index = SqliteIndex(tmp_path / "data" / "library.db")
    try:
        index.upsert_material(
            material_id="m-a", course_id="c-ref", filename="a.pptx", kind="pptx", pages=3, status="parsed", checksum=""
        )
        index.index_chunks("m-a", [{"page": 2, "heading": "核心", "text": "第二页原文", "source": "text"}])
        index.upsert_material(
            material_id="m-other", course_id="c-other", filename="b.pptx", kind="pptx", pages=3, status="parsed", checksum=""
        )
        index.index_chunks("m-other", [{"page": 2, "heading": "", "text": "别的课的内容", "source": "text"}])

        scoped = build_registry(index=index, course_id="c-ref", material_ids=["m-a"])
        read = scoped.get("read_material_slice").handler
        result = read(material_id="m-a", page=2)
        assert "第二页原文" in result["text"]

        blocked = read(material_id="m-other", page=2)
        assert "error" in blocked  # 不在本节指定范围内

        search = scoped.get("search_materials").handler
        hits = search(query="内容", k=5)
        assert all(item["material_id"] == "m-a" for item in hits)
    finally:
        index.close()


# --- C 课件覆盖自检 ---------------------------------------------------------
def test_reference_coverage_flags_missing_topics() -> None:
    digest = [
        {"material_id": "m-a", "filename": "课件.pptx", "locator": "#s1", "heading": "梯度下降", "gist": "…"},
        {"material_id": "m-a", "filename": "课件.pptx", "locator": "#s2", "heading": "学习率", "gist": "…"},
    ]
    blocks = [{"type": "text", "md": "本节讲梯度下降的原理。"}]
    missing = TeacherAgent.reference_coverage_issues(blocks, digest)
    assert len(missing) == 1
    assert "学习率" in missing[0]
    assert "梯度下降" not in missing[0]


def test_reference_coverage_ignores_pages_without_heading() -> None:
    digest = [{"material_id": "m-a", "filename": "课件.pptx", "locator": "#s7", "heading": "", "gist": "某页正文"}]
    assert TeacherAgent.reference_coverage_issues([{"type": "text", "md": "随便讲讲"}], digest) == []


class _FakeTeacher:
    """两轮替身：第一轮故意漏掉课件主题，补讲轮才补上。"""

    def __init__(self) -> None:
        self.contexts: list[dict] = []

    async def teach(self, context, **kwargs):  # noqa: ANN003 - 与真实 Agent 同形
        self.contexts.append(dict(context))
        if context.get("coverage_note"):
            body = "补充：学习率决定了每一步走多大。"
        else:
            body = "本节讲梯度下降的原理。"
        yield (
            '{"type":"meta","lesson_id":"L1-1","depth":"standard","version":1}\n'
            + json.dumps({"type": "text", "md": body}, ensure_ascii=False)
            + '\n{"type":"done","tokens":0}\n'
        )


def test_generate_script_runs_reference_coverage_round(tmp_path: Path) -> None:
    course_dir = _make_course(tmp_path, pages=3)
    lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一节"
    digest = [
        {"material_id": "m-a", "filename": "课件.pptx", "locator": "#s1", "heading": "梯度下降", "gist": "…"},
        {"material_id": "m-a", "filename": "课件.pptx", "locator": "#s2", "heading": "学习率", "gist": "…"},
    ]
    teacher = _FakeTeacher()
    outcome = asyncio.run(
        generate_script(
            lesson_dir,
            teacher=teacher,
            context={
                "lesson": {"id": "L1-1", "title": "第一节"},
                "chapter": {"id": "C1", "title": "第一章"},
                "course": {"name": "课件课"},
                "knowledge_points": [],
                "reference_digest": digest,
            },
            coverage_rounds=0,
            reference_coverage_rounds=1,
        )
    )
    assert len(teacher.contexts) == 2  # 触发了一轮补讲
    assert "学习率" in str(teacher.contexts[1].get("coverage_note") or "")
    assert outcome.reference_missing == []


def test_generate_script_skips_coverage_round_when_too_many_missing(tmp_path: Path) -> None:
    """缺得太多 = 整体没对上课件，硬补一轮只会更啰嗦；如实报给用户。"""
    course_dir = _make_course(tmp_path, pages=3)
    lesson_dir = course_dir / "chapters" / "C1-第一章" / "lessons" / "L1-1-第一节"
    digest = [
        {"material_id": "m-a", "filename": "课件.pptx", "locator": f"#s{i}", "heading": f"主题{i:02d}", "gist": "…"}
        for i in range(1, 21)
    ]
    teacher = _FakeTeacher()
    outcome = asyncio.run(
        generate_script(
            lesson_dir,
            teacher=teacher,
            context={
                "lesson": {"id": "L1-1", "title": "第一节"},
                "chapter": {"id": "C1", "title": "第一章"},
                "course": {"name": "课件课"},
                "knowledge_points": [],
                "reference_digest": digest,
            },
            coverage_rounds=0,
            reference_coverage_rounds=1,
        )
    )
    assert len(teacher.contexts) == 1  # 没有补讲轮
    assert len(outcome.reference_missing) == 20


# --- 提示词排版 -------------------------------------------------------------
@pytest.mark.no_network
def test_teacher_prompt_renders_digest_and_tool_hint() -> None:
    teacher = TeacherAgent(client=object())
    prompt = teacher.build_user_prompt(
        {
            "lesson": {"id": "L1-1", "title": "第一节"},
            "chapter": {"id": "C1", "title": "第一章"},
            "course": {"name": "课件课"},
            "knowledge_points": [],
            "reference_digest": [
                {
                    "material_id": "m-a",
                    "filename": "课件.pptx",
                    "locator": "#s3",
                    "page": 3,
                    "heading": "核心",
                    "gist": "只有前者的列数等于后者的行数时才能相乘。",
                }
            ],
            "bound_references": [],
            "tools_available": True,
        }
    )
    assert "资料全貌" in prompt
    assert "[m-a] 课件.pptx#s3 核心：只有前者的列数等于后者的行数时才能相乘。" in prompt
    assert "read_material_slice" in prompt
