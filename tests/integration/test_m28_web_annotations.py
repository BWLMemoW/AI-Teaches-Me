"""M28 集成测试：真实 QtWebEngine（离屏）里验证讲义标注与人设徽章。

渲染内核是 JS，单元测试只能查字符串；这里真的把页面跑起来，确认：
* 人设徽章画得出来；
* 标注能把原文片段高亮回去；
* 划词浮出的工具条能产生事件，并被 ``drainEvents()`` 取走（Python 侧靠它收事件）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LESSON_HTML = REPO_ROOT / "app" / "web" / "lesson.html"

pytestmark = pytest.mark.integration


def _evaluate(qtbot, view, script: str, *, timeout: int = 45_000):
    """执行一段 JS 并把返回值同步取回来（每次用独立的 key，避免旧值干扰）。"""
    holder: dict[str, object] = {}
    view.page().runJavaScript(script, lambda value: holder.update(value=value))
    qtbot.waitUntil(lambda: "value" in holder, timeout=timeout)
    return holder["value"]


def _load_page(qtbot, view) -> None:
    loaded: dict[str, bool] = {}
    view.loadFinished.connect(lambda ok: loaded.update(done=bool(ok)))
    from PySide6.QtCore import QUrl

    view.load(QUrl.fromLocalFile(str(LESSON_HTML)))
    qtbot.waitUntil(lambda: "done" in loaded, timeout=30_000)
    assert loaded["done"] is True


BLOCKS = [
    {"type": "meta", "lesson_id": "L1-1", "depth": "standard"},
    {"type": "text", "md": "## 矩阵乘法\n\n矩阵 A 为 m×n，矩阵 B 为 n×p。"},
    {"type": "done", "tokens": 0},
]


def test_persona_badge_and_annotation_highlight(qtbot) -> None:
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWebEngineWidgets import QWebEngineView

    view = QWebEngineView()
    qtbot.addWidget(view)
    _load_page(qtbot, view)

    script = (
        "window.ATM.setPersona({id:'catgirl-assistant',name:'猫娘助教',avatar:'🐱',tone:['活泼','亲切']});"
        + "window.ATM.renderBlocks(" + json.dumps(BLOCKS, ensure_ascii=False) + ");"
        + "window.ATM.setAnnotations([{id:'an-1',quote:'矩阵 A 为 m×n',note:'先看维度',color:'yellow'}]);"
        + "JSON.stringify({"
        + "persona: document.querySelector('.atm-persona') ? document.querySelector('.atm-persona').textContent : '',"
        + "marks: document.querySelectorAll('mark.atm-annotation').length,"
        + "mark: document.querySelector('mark.atm-annotation') ? document.querySelector('mark.atm-annotation').textContent : '',"
        + "title: document.querySelector('mark.atm-annotation') ? document.querySelector('mark.atm-annotation').getAttribute('title') : '',"
        + "has_note: document.querySelector('mark.atm-annotation') ? document.querySelector('mark.atm-annotation').getAttribute('data-has-note') : ''"
        + "})"
    )
    payload = json.loads(str(_evaluate(qtbot, view, script)))

    assert "猫娘助教" in payload["persona"]
    assert "活泼" in payload["persona"]
    assert payload["marks"] == 1
    assert payload["mark"] == "矩阵 A 为 m×n"
    assert "先看维度" in payload["title"]  # 悬停提示：评论 + 「点击可改评论 / 引用到追问」
    assert payload["has_note"] == "1"

    # 再点一次渲染（换主题 / 刷新讲义）后，高亮仍然在
    again = json.loads(
        str(
            _evaluate(
                qtbot,
                view,
                "window.ATM.renderBlocks(" + json.dumps(BLOCKS, ensure_ascii=False) + ");"
                "JSON.stringify({marks: document.querySelectorAll('mark.atm-annotation').length})",
            )
        )
    )
    assert again["marks"] == 1


def test_selection_toolbar_emits_annotation_event(qtbot) -> None:
    """划词 → 浮出工具条 → 点「标注」→ 事件进队列（Python 侧靠 drainEvents 取走）。"""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWebEngineWidgets import QWebEngineView

    view = QWebEngineView()
    qtbot.addWidget(view)
    _load_page(qtbot, view)

    _evaluate(
        qtbot,
        view,
        "window.ATM.renderBlocks(" + json.dumps(BLOCKS, ensure_ascii=False) + "); 'ok'",
    )

    _evaluate(
        qtbot,
        view,
        """
        (function () {
          var node = document.querySelector(".atm-text p") || document.querySelector(".atm-text");
          var range = document.createRange();
          range.selectNodeContents(node);
          var selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          document.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
          return "selected";
        })()
        """,
    )

    # 工具条是延迟 10ms 浮出的，等它出现
    qtbot.waitUntil(
        lambda: _evaluate(qtbot, view, "document.querySelectorAll('.atm-annotbar-button').length")
        not in (None, 0),
        timeout=15_000,
    )
    assert _evaluate(qtbot, view, "document.querySelector('.atm-annotbar').textContent") == "✏️ 标注💬 追问"

    _evaluate(qtbot, view, "document.querySelectorAll('.atm-annotbar-button')[0].click(); 'clicked'")

    # drainEvents() 返回 JSON 字符串：QtWebEngine 只回传基本类型，JS 数组会变成空串
    events = json.loads(str(_evaluate(qtbot, view, "window.ATM.drainEvents()")))
    assert len(events) == 1
    assert events[0]["action"] == "annotate"
    assert "矩阵 A 为 m×n" in events[0]["quote"]
    assert events[0]["block_type"] == "text"

    # 取走之后队列必须为空，否则 UI 会重复标注
    assert json.loads(str(_evaluate(qtbot, view, "window.ATM.drainEvents()"))) == []


def test_event_queue_is_empty_when_nothing_happens(qtbot) -> None:
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWebEngineWidgets import QWebEngineView

    view = QWebEngineView()
    qtbot.addWidget(view)
    _load_page(qtbot, view)

    raw = _evaluate(qtbot, view, "window.ATM.drainEvents()")
    # 必须是字符串：QtWebEngine 只回传基本类型，返回数组会变成空串（曾导致标注点了没反应）
    assert isinstance(raw, str)
    assert json.loads(raw) == []


def test_webengine_renderer_bridge_really_delivers_events(qtbot) -> None:
    """回归：``WebEngineRenderer.drain_events`` 必须把讲义页的事件真的送到 Python。"""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from app.ui.pages.study_page import WebEngineRenderer, default_html_path

    renderer = WebEngineRenderer()
    qtbot.addWidget(renderer.widget())
    renderer.load_page(default_html_path())
    qtbot.waitUntil(lambda: renderer.ready, timeout=30_000)

    renderer.evaluate("window.ATM.pushEvent({action:'ask_quote', quote:'矩阵 A 为 m×n'});")

    received: dict[str, object] = {}
    renderer.drain_events(lambda events: received.update(events=events))
    qtbot.waitUntil(lambda: "events" in received, timeout=15_000)

    assert received["events"] == [{"action": "ask_quote", "quote": "矩阵 A 为 m×n"}]

    # 取走后队列必须为空，避免重复标注
    again: dict[str, object] = {}
    renderer.drain_events(lambda events: again.update(events=events))
    qtbot.waitUntil(lambda: "events" in again, timeout=15_000)
    assert again["events"] == []


IMAGE_BLOCKS = [
    {"type": "meta", "lesson_id": "L1-1"},
    {"type": "image", "path": "C:/tmp/atm-figure.png", "caption": "矩阵乘法的形状示意"},
    {"type": "text", "md": "矩阵 A 为 m×n。"},
    {"type": "done", "tokens": 0},
]


def test_image_block_offers_cite_to_question(qtbot) -> None:
    """图片块带「引用到追问」按钮，点一下把图（含本地路径）送进事件队列。"""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWebEngineWidgets import QWebEngineView

    view = QWebEngineView()
    qtbot.addWidget(view)
    _load_page(qtbot, view)

    _evaluate(
        qtbot,
        view,
        "window.ATM.renderBlocks(" + json.dumps(IMAGE_BLOCKS, ensure_ascii=False) + "); 'ok'",
    )
    assert _evaluate(qtbot, view, "document.querySelectorAll('.atm-cite-button').length") == 1

    _evaluate(qtbot, view, "document.querySelector('.atm-cite-button').click(); 'clicked'")
    events = json.loads(str(_evaluate(qtbot, view, "window.ATM.drainEvents()")))
    assert len(events) == 1
    assert events[0]["action"] == "cite_block"
    assert events[0]["block_type"] == "image"
    assert events[0]["asset"] == "C:/tmp/atm-figure.png"
    assert "矩阵乘法的形状示意" in events[0]["quote"]
    assert "C:/tmp/atm-figure.png" in events[0]["quote"]


def test_clicking_annotation_mark_asks_for_dialog(qtbot) -> None:
    """点讲义里的黄色划线 → 事件通知主线程打开「写评论 / 引用」窗口。"""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWebEngineWidgets import QWebEngineView

    view = QWebEngineView()
    qtbot.addWidget(view)
    _load_page(qtbot, view)

    _evaluate(
        qtbot,
        view,
        "window.ATM.renderBlocks(" + json.dumps(BLOCKS, ensure_ascii=False) + ");"
        "window.ATM.setAnnotations([{id:'an-9',quote:'矩阵 A 为 m×n',note:'先看维度',color:'yellow'}]); 'ok'",
    )
    assert _evaluate(qtbot, view, "document.querySelectorAll('mark.atm-annotation').length") == 1
    assert _evaluate(qtbot, view, "document.querySelector('mark.atm-annotation').getAttribute('data-has-note')") == "1"

    _evaluate(qtbot, view, "document.querySelector('mark.atm-annotation').click(); 'clicked'")
    events = json.loads(str(_evaluate(qtbot, view, "window.ATM.drainEvents()")))
    assert events == [{"action": "open_annotation", "annotation_id": "an-9"}]


def test_hover_annotation_shows_comment_and_cite_bubble(qtbot) -> None:
    """鼠标停在黄色划线上 → 浮出气泡，里面是「评论」「引用到追问」两个动作。"""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWebEngineWidgets import QWebEngineView

    view = QWebEngineView()
    qtbot.addWidget(view)
    _load_page(qtbot, view)

    _evaluate(
        qtbot,
        view,
        "window.ATM.renderBlocks(" + json.dumps(BLOCKS, ensure_ascii=False) + ");"
        "window.ATM.setAnnotations([{id:'an-7',quote:'矩阵 A 为 m×n',note:'先看维度',color:'yellow'}]); 'ok'",
    )
    _evaluate(
        qtbot,
        view,
        """
        (function () {
          var mark = document.querySelector("mark.atm-annotation");
          mark.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
          return "hovered";
        })()
        """,
    )

    bubble = _evaluate(qtbot, view, "document.querySelectorAll('.atm-annotbubble').length")
    assert bubble == 1
    assert _evaluate(qtbot, view, "document.querySelector('.atm-annotbubble-note').textContent") == "先看维度"
    labels = _evaluate(
        qtbot,
        view,
        "Array.prototype.map.call(document.querySelectorAll('.atm-annotbubble-button'), function (b) { return b.textContent; }).join('|')",
    )
    assert "评论" in labels and "引用到追问" in labels

    # 气泡里点「引用到追问」→ 事件里带着这条标注的 id（主线程据此带上评论）
    _evaluate(
        qtbot,
        view,
        """
        (function () {
          var buttons = document.querySelectorAll(".atm-annotbubble-button");
          for (var i = 0; i < buttons.length; i += 1) {
            if (buttons[i].textContent.indexOf("引用") >= 0) { buttons[i].click(); return "cited"; }
          }
          return "missing";
        })()
        """,
    )
    events = json.loads(str(_evaluate(qtbot, view, "window.ATM.drainEvents()")))
    assert events == [{"action": "cite_annotation", "annotation_id": "an-7"}]


MARKDOWN_BLOCKS = [
    {"type": "meta", "lesson_id": "L1-1"},
    # 段落里故意带 Markdown（加粗 / 行内代码）和源文本换行
    {
        "type": "text",
        "md": "矩阵 **A** 为 m×n，`B` 为 n×p，\n只有 `前者的列数` 等于后者的行数时才能相乘。",
    },
    {"type": "text", "md": "行内公式 $m \\times n$ 也要能划上。"},
    {"type": "callout", "style": "tip", "md": "**提示**：先看维度。"},
    {"type": "done", "tokens": 0},
]


def test_annotation_matches_markdown_formatted_text(qtbot) -> None:
    """回归：带 Markdown / 公式的句子以前「划不上线」，因为 DOM 里的换行与
    KaTeX 隐藏的 LaTeX 原文让 indexOf 匹配失败；这里要求它们都能出划线。"""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWebEngineWidgets import QWebEngineView

    view = QWebEngineView()
    qtbot.addWidget(view)
    _load_page(qtbot, view)

    quotes = [
        "矩阵 A 为 m×n，B 为 n×p， 只有 前者的列数 等于后者的行数时才能相乘。",
        "只有 前者的列数 等于后者的行数时才能相乘。",
        "m×n 也要能划上。",
        "提示：先看维度。",
    ]
    annotations = [
        {"id": f"an-{index}", "quote": quote, "note": "", "color": "yellow"}
        for index, quote in enumerate(quotes)
    ]
    payload = json.loads(
        str(
            _evaluate(
                qtbot,
                view,
                "window.ATM.renderBlocks(" + json.dumps(MARKDOWN_BLOCKS, ensure_ascii=False) + ");"
                + "window.ATM.setAnnotations(" + json.dumps(annotations, ensure_ascii=False) + ");"
                + "JSON.stringify({marks: document.querySelectorAll('mark.atm-annotation').length,"
                + "ids: Array.prototype.map.call(document.querySelectorAll('mark.atm-annotation'), function (m) {"
                + "  return m.getAttribute('data-annotation-id'); })"
                + "})",
            )
        )
    )

    assert payload["marks"] >= len(quotes)
    marked = set(payload["ids"])
    assert "an-0" in marked  # 带加粗 / 行内代码 / 源文本换行的整句
    assert "an-1" in marked  # 从句
    assert "an-2" in marked  # 行内公式之后的文字（KaTeX）
    assert "an-3" in marked  # callout 里的加粗
