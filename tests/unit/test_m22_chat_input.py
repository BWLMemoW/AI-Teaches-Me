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


def test_chat_history_is_not_focusable(qtbot) -> None:
    """回归：记录区（大框）曾被误当成输入框 → 现在不可获焦且明确标注「只读」。"""
    drawer = ChatDrawer()
    qtbot.addWidget(drawer)

    assert drawer.history.focusPolicy() == Qt.FocusPolicy.NoFocus
    # 记录区是滚动展示区，不该具备任何「输入」能力
    assert not hasattr(drawer.history, "setPlainText")
    assert drawer.input_edit.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert "只读" in drawer.history_label.text() or "记录" in drawer.history_label.text()


def test_chat_input_is_attached_to_send_button(qtbot) -> None:
    """输入框与发送按钮同一行（普通聊天应用的布局）。"""
    from PySide6.QtWidgets import QPushButton

    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.show()

    assert drawer.send_button.text() == "发送"
    assert drawer.input_edit.parent() is not None
    assert isinstance(drawer.send_button, QPushButton)