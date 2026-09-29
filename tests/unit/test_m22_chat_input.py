"""M22 测试：追问输入框可输入 + Ctrl+Enter 发送。"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from app.ui.widgets.chat_drawer import ChatDrawer

pytestmark = pytest.mark.ui


def test_chat_input_accepts_typing(qtbot) -> None:
    """回归：追问输入框曾出现「文字输不进去」。"""
    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.show()
    drawer.focus_input()

    assert drawer.input_edit.isEnabled() is True
    assert drawer.input_edit.isReadOnly() is False
    assert drawer.input_edit.focusPolicy() == Qt.FocusPolicy.StrongFocus

    qtbot.keyClicks(drawer.input_edit, "why is it so")
    assert drawer.input_edit.toPlainText() == "why is it so"


def test_chat_input_ctrl_enter_sends(qtbot) -> None:
    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.show()
    drawer.input_edit.setPlainText("这两处是同一回事吗？")

    with qtbot.waitSignal(drawer.send_requested, timeout=3000) as blocker:
        qtbot.keyClick(drawer.input_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)

    assert blocker.args == ["这两处是同一回事吗？", ""]
    assert drawer.input_edit.toPlainText() == ""  # 发送后清空
    assert "我：这两处是同一回事吗？" in drawer.history.toPlainText()
