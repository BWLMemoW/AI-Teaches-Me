"""应用入口（《项目文档》§12 的 ``app/main.py``）。

用法：

    python -m app.main                       # 正常启动（需要图形环境）
    python -m app.main --self-check          # 无界面自检：引导 → 主题 → 示例课程 → 渲染内核
    python -m app.main --self-check --no-web # 只做非 Web 部分的自检

自检对应 M0 出口标准：能配置 API 并完成一次流式对话（流式由契约测试覆盖）、
主题可切换、示例课程可离线阅读。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .bootstrap import BootstrapResult, bootstrap_workspace, build_services
from .core.events import EventBus
from .infra.secret_store import SecretStore
from .services.settings_service import SettingsService
from .ui.theme.presets import THEME_KEYS

APP_TITLE = "AI Teaches Me"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atm", description=f"{APP_TITLE}（PC 桌面客户端）")
    parser.add_argument("--workspace", type=str, default=None, help="工作区根目录（默认仓库根目录）")
    parser.add_argument("--theme", type=str, default=None, choices=list(THEME_KEYS), help="启动主题")
    parser.add_argument("--self-check", action="store_true", help="跑一遍自检后退出（无界面）")
    parser.add_argument("--no-web", action="store_true", help="自检时不启动 QtWebEngine")
    return parser


def prepare_bootstrap(args: argparse.Namespace) -> BootstrapResult:
    result = bootstrap_workspace(args.workspace)
    if args.theme:
        result.config.ui.theme = args.theme
        from .core.config import save_config

        save_config(result.layout.root, result.config)
    return result


def _pump(app, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def run_self_check(args: argparse.Namespace) -> int:
    """无界面自检：把 M0 的出口标准逐条跑一遍，打印 JSON 报告。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
    from PySide6.QtWidgets import QApplication

    from .services import course_service, lesson_service
    from .ui.main_window import MainWindow

    report: dict[str, object] = {}
    errors: list[str] = []

    result = prepare_bootstrap(args)
    report["workspace"] = str(result.layout.root)
    report["read_only"] = result.read_only
    report["needs_api_setup"] = result.needs_api_setup
    report["seeded_files"] = len(result.seeded_files)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    services = build_services(result)
    window = MainWindow(result, services=services, bus=EventBus())
    _pump(app, 0.2)

    themes_tried: list[str] = []
    for key in THEME_KEYS:
        services.settings.set_theme(key)
        window.apply_current_theme()
        themes_tried.append(key)
        _pump(app, 0.05)
    report["themes"] = themes_tried
    services.settings.set_theme(result.config.ui.theme)

    courses = course_service.scan_courses(result.layout.courses)
    report["courses"] = [item.name for item in courses]
    if not courses:
        errors.append("课程库为空：示例课程未装载")
    else:
        summary = courses[0]
        window._open_course(summary)  # noqa: SLF001 - 自检直接用内部入口
        _pump(app, 1.0)
        content = window.study_page.last_content
        if content is None:
            errors.append("示例课程讲义未加载")
        else:
            report["lesson"] = {"id": content.lesson_id, "blocks": len(content.blocks)}
            if not lesson_service.load_lesson(content.lesson_dir).has_blocks:
                errors.append("示例课程缺少结构化内容块")

    if not args.no_web:
        renderer = window.study_page.renderer
        state: dict[str, object] = {}

        def on_state(value: object) -> None:
            state["value"] = value

        try:
            # 等 lesson.html 真正 loadFinished 后再取状态（离屏 + 无 GPU 时首次加载较慢）
            deadline = time.time() + 25
            while not getattr(renderer, "ready", False) and time.time() < deadline:
                app.processEvents()
                time.sleep(0.05)
            report["web_ready"] = bool(getattr(renderer, "ready", False))
            report["web_load_ok"] = getattr(renderer, "last_load_ok", None)
            if not getattr(renderer, "ready", False):
                errors.append("lesson.html 未加载成功（QtWebEngine 未就绪）")
            renderer.evaluate("JSON.stringify(window.ATM.getState())", on_state)  # type: ignore[attr-defined]
            deadline = time.time() + 20
            while "value" not in state and time.time() < deadline:
                app.processEvents()
                time.sleep(0.05)
            raw = state.get("value")
            if isinstance(raw, str):
                web_state = json.loads(raw)
                report["web"] = web_state
                if not web_state.get("blocks"):
                    errors.append("渲染内核没有渲染出内容块")
                missing_vendor = [name for name, ok in (web_state.get("vendor") or {}).items() if not ok]
                if missing_vendor:
                    errors.append("本地渲染依赖缺失：" + "、".join(missing_vendor))
            else:
                errors.append(f"无法从渲染内核取回状态：{raw!r}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Web 渲染自检异常：{exc}")

    window.close()
    _pump(app, 0.1)

    report["errors"] = errors
    report["ok"] = not errors
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.self_check:
        return run_self_check(args)

    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_TITLE)

    result = prepare_bootstrap(args)
    services = build_services(result)
    window = MainWindow(result, services=services)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
