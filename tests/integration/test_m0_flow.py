"""M0 端到端集成测试（《项目文档》§15.1 集成测试层 / §16.1 M0 出口标准）。

覆盖三件事：

1. 「配置 → 流式对话」闭环：用桩 provider 跑通 ``stream_chat`` + NDJSON 解析；
2. 示例课程离线可读：读取预置 ``script.blocks.jsonl`` 并校验协议；
3. 渲染内核：在真实 QtWebEngine（离屏）里加载 lesson.html，渲染内容块并读回状态。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from app.bootstrap import bootstrap_workspace
from app.core.errors import AuthError
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.llm.client import LLMClient
from app.llm.stream import BlockStreamParser
from app.services import lesson_service
from app.services.settings_service import SettingsService

pytestmark = pytest.mark.integration

SEED_LESSON = Path("Courses") / "ATM 使用指南" / "chapters" / "C1-三分钟上手" / "lessons" / "L1-1-先配置你的-AI"


def _chunk(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None)


class _FakeStream:
    def __init__(self, pieces):
        self._pieces = list(pieces)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._pieces:
            return _chunk(self._pieces.pop(0))
        raise StopAsyncIteration


class _FakeCompletions:
    def __init__(self, pieces):
        self._pieces = pieces

    async def create(self, **kwargs):
        return _FakeStream(self._pieces)


def _bootstrap(tmp_path: Path):
    return bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )


def test_config_to_streaming_chat_closure(tmp_path: Path) -> None:
    """配置档案 + 密钥 → 流式对话 → NDJSON 解析出内容块（M0 出口标准第 1 条）。"""
    result = _bootstrap(tmp_path)
    store = SecretStore(backend=InMemoryBackend())
    service = SettingsService(result.config, workspace_root=tmp_path, store=store)
    service.set_api_key("deepseek-default", "sk-integration-test")
    service.save()

    ndjson = "\n".join(
        [
            '{"type":"meta","lesson_id":"L1-1","depth":"minimal","version":1}',
            '{"type":"text","md":"## 你好，矩阵"}',
            '{"type":"done","tokens":12}',
        ]
    )
    pieces = [ndjson[i : i + 17] for i in range(0, len(ndjson), 17)]

    client = LLMClient(
        result.config,
        secret_store=store,
        client_factory=lambda profile, key: SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeCompletions(pieces)),
            models=SimpleNamespace(),
        ),
        env={},
    )

    async def run() -> list[str]:
        return [piece async for piece in client.stream_chat([{"role": "user", "content": "讲一节"}])]

    text = "".join(asyncio.run(run()))
    parser = BlockStreamParser()
    blocks = parser.feed(text)
    blocks.extend(parser.finish())

    assert [block["type"] for block in blocks] == ["meta", "text", "done"]
    assert parser.validate_protocol() == []
    assert client.usage.total()["calls"] == 1


def test_config_without_key_reports_actionable_error(tmp_path: Path) -> None:
    result = _bootstrap(tmp_path)
    client = LLMClient(result.config, secret_store=SecretStore(backend=InMemoryBackend()), env={})

    async def run() -> None:
        async for _ in client.stream_chat([{"role": "user", "content": "hi"}]):
            pass

    with pytest.raises(AuthError) as excinfo:
        asyncio.run(run())

    assert "设置页" in excinfo.value.explain()


def test_seed_course_is_readable_offline(tmp_path: Path) -> None:
    """示例课程可离线阅读（M0 出口标准第 3 条）。"""
    _bootstrap(tmp_path)
    lesson_dir = tmp_path / SEED_LESSON

    content = lesson_service.load_lesson(lesson_dir)

    assert content.has_blocks is True
    assert content.blocks[0]["type"] == "meta"
    assert content.blocks[-1]["type"] == "done"
    assert "三步接上 AI" in content.markdown
    parser = BlockStreamParser()
    parser.feed("\n".join(json.dumps(block, ensure_ascii=False) for block in content.blocks) + "\n")
    parser.finish()
    assert parser.validate_protocol() == []


def test_render_kernel_renders_blocks_in_real_webengine(qtbot, tmp_path: Path) -> None:
    """真实 QtWebEngine（离屏）里渲染内容块，并确认本地 vendor 全部生效。"""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtCore import QUrl
    from PySide6.QtWebEngineWidgets import QWebEngineView

    _bootstrap(tmp_path)
    content = lesson_service.load_lesson(tmp_path / SEED_LESSON)
    html_path = Path(__file__).resolve().parents[2] / "app" / "web" / "lesson.html"

    view = QWebEngineView()
    qtbot.addWidget(view)
    state: dict[str, object] = {}
    view.loadFinished.connect(lambda ok: state.update(loaded=bool(ok)))
    view.load(QUrl.fromLocalFile(str(html_path)))
    qtbot.waitUntil(lambda: "loaded" in state, timeout=30_000)
    assert state["loaded"] is True

    script = (
        "window.ATM.renderBlocks("
        + json.dumps(list(content.blocks), ensure_ascii=False)
        + "); JSON.stringify(window.ATM.getState())"
    )
    view.page().runJavaScript(script, lambda value: state.update(value=value))
    qtbot.waitUntil(lambda: "value" in state, timeout=20_000)

    web = json.loads(str(state["value"]))
    assert web["blocks"] == len(content.blocks) - 1  # done 块不计数
    assert all(web["vendor"].values()), web["vendor"]
    assert web["meta"]["lesson_id"] == "L1-1"
    assert web["textLength"] > 200

    text_holder: dict[str, str] = {}
    view.page().runJavaScript(
        "document.getElementById('atm-root').innerText",
        lambda value: text_holder.update(text=str(value)),
    )
    qtbot.waitUntil(lambda: "text" in text_holder, timeout=10_000)
    assert "三步接上 AI" in text_holder["text"]
    assert "你想改的东西" in text_holder["text"]  # 表格块渲染成功
