"""课程图标服务（M8）：预设图标 / 自定义 SVG / AI 生成，**全部离线**。

* 预设图标：``app/ui/assets/icons/presets/*.svg``（单色 ``currentColor``，用 ``color`` 染色）；
* AI 生成：走 ``IllustratorAgent`` 的 Render-and-Review + 清洗，失败自动降级到预设；
* 落盘：统一写 ``Courses/<课>/assets/icon.svg``，并把 ``icon`` 写进 ``course.json``。

服务层不导入 Qt（§1.5 红线 2）；把 SVG 渲染成位图由 UI 层负责。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..core.models import CourseIcon
from ..infra import file_repository
from ..infra.svg_sanitizer import clean, is_acceptable

PRESET_DIR = Path(__file__).resolve().parents[1] / "ui" / "assets" / "icons" / "presets"
ICON_RELATIVE_PATH = "assets/icon.svg"
DEFAULT_ICON = CourseIcon(kind="preset", value="general")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class IconResult:
    icon: CourseIcon
    svg: str = ""
    path: Path | None = None
    generated_by_ai: bool = False
    issues: list[str] = field(default_factory=list)


def preset_ids() -> list[str]:
    if not PRESET_DIR.is_dir():
        return []
    return sorted(item.stem for item in PRESET_DIR.glob("*.svg"))


def preset_label(icon_id: str) -> str:
    path = PRESET_DIR / f"{icon_id}.svg"
    if not path.exists():
        return icon_id
    match = TITLE_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1).strip() if match else icon_id


def preset_choices() -> list[tuple[str, str]]:
    """（id, 中文名）列表，供界面选择器使用。"""
    return [(item, preset_label(item)) for item in preset_ids()]


def load_preset_svg(icon_id: str) -> str:
    path = PRESET_DIR / f"{icon_id}.svg"
    if not path.exists():
        path = PRESET_DIR / "general.svg"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def tint_svg(svg_text: str, color: str) -> str:
    """把单色 SVG 的 ``currentColor`` 换成指定颜色（不含则补到根元素上）。"""
    text = str(svg_text or "")
    if "currentColor" in text:
        return text.replace("currentColor", color)
    if "<svg" in text:
        return text.replace("<svg", f'<svg stroke="{color}"', 1)
    return text


def read_icon_svg(course_dir: str | Path, icon: CourseIcon | None) -> str:
    """取图标 SVG 文本（预设直接读模板；自绘 / AI 读课程资产）。"""
    target = icon or DEFAULT_ICON
    if target.kind == "preset":
        return tint_svg(load_preset_svg(target.value), target.color)
    path = Path(course_dir) / (target.value or ICON_RELATIVE_PATH)
    if path.exists():
        return tint_svg(path.read_text(encoding="utf-8", errors="replace"), target.color)
    return tint_svg(load_preset_svg("general"), target.color)


def save_icon_svg(course_dir: str | Path, svg_text: str, *, keep_history: bool = False) -> Path:
    """把图标写入 ``assets/icon.svg``；``keep_history`` 时旧图标存成 ``icon.vK.svg``。"""
    target = Path(course_dir) / ICON_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if keep_history and target.exists():
        version = 1
        while (target.parent / f"icon.v{version}.svg").exists():
            version += 1
        shutil.copy2(target, target.parent / f"icon.v{version}.svg")
    file_repository.write_text(target, svg_text)
    return target


class IconService:
    """图标的选择 / 生成 / 落盘。"""

    def __init__(self, *, client: object | None = None, illustrator: object | None = None) -> None:
        self.client = client
        self._illustrator = illustrator

    def _illustrator_or_default(self):
        if self._illustrator is not None:
            return self._illustrator
        if self.client is None:
            return None
        from ..agents.illustrator import IllustratorAgent

        return IllustratorAgent(self.client)

    # --- 预设 -----------------------------------------------------------
    def apply_preset(self, course_dir: str | Path, icon_id: str, *, color: str = "#E86A92", bg: str = "#FFF5F8") -> IconResult:
        icon = CourseIcon(kind="preset", value=icon_id if icon_id in preset_ids() else "general", color=color, bg=bg)
        svg = tint_svg(load_preset_svg(icon.value), icon.color)
        path = save_icon_svg(course_dir, svg)
        return IconResult(icon=icon, svg=svg, path=path)

    # --- AI 生成 --------------------------------------------------------
    async def generate_with_ai(
        self,
        course_dir: str | Path,
        *,
        concept: str,
        color: str = "#E86A92",
        bg: str = "#FFF5F8",
        fallback_preset: str = "ai",
    ) -> IconResult:
        """用图示 Agent 生成课程图标；失败或不合格时降级到预设（不阻塞建课）。"""
        from ..agents.illustrator import SvgSpec

        illustrator = self._illustrator_or_default()
        if illustrator is None:
            result = self.apply_preset(course_dir, fallback_preset, color=color, bg=bg)
            result.issues.append("没有可用的模型配置，已改用预设图标")
            return result

        spec = SvgSpec(
            concept=f"课程图标：{concept}。极简线性风格，单色描边（用 currentColor），64×64 viewBox，"
                    "不要文字标题以外的装饰，不要渐变与阴影",
            title=f"{concept} 图标",
            style="极简、线性、单色",
            caption_md=f"（AI 生成的课程图标：{concept}）",
        )
        outcome = await illustrator.make_svg(spec)
        if not outcome.ok:
            result = self.apply_preset(course_dir, fallback_preset, color=color, bg=bg)
            result.issues.extend(outcome.issues or ["AI 生成的图标未通过自检"])
            return result

        candidate = clean(outcome.svg)
        if not is_acceptable(candidate):
            result = self.apply_preset(course_dir, fallback_preset, color=color, bg=bg)
            result.issues.extend(candidate.problems)
            return result

        svg = tint_svg(candidate.svg, color)
        path = save_icon_svg(course_dir, svg, keep_history=True)
        return IconResult(
            icon=CourseIcon(kind="ai", value=ICON_RELATIVE_PATH, color=color, bg=bg),
            svg=svg,
            path=path,
            generated_by_ai=True,
        )

    # --- 自定义 SVG -----------------------------------------------------
    def apply_svg(self, course_dir: str | Path, svg_text: str, *, color: str = "#E86A92", bg: str = "#FFF5F8") -> IconResult:
        candidate = clean(svg_text)
        if not is_acceptable(candidate):
            result = self.apply_preset(course_dir, "general", color=color, bg=bg)
            result.issues.extend(candidate.problems)
            return result
        svg = tint_svg(candidate.svg, color)
        path = save_icon_svg(course_dir, svg, keep_history=True)
        return IconResult(icon=CourseIcon(kind="svg", value=ICON_RELATIVE_PATH, color=color, bg=bg), svg=svg, path=path)

    # --- 记录到 course.json ---------------------------------------------
    @staticmethod
    def store(course_dir: str | Path, icon: CourseIcon) -> dict:
        from . import course_service

        return course_service.update_course_meta(course_dir, icon=icon.model_dump(mode="json"))

    @staticmethod
    def load(course_dir: str | Path) -> CourseIcon:
        data = file_repository.read_json_or_default(Path(course_dir) / "course.json", {}) or {}
        raw = data.get("icon")
        if isinstance(raw, dict):
            try:
                return CourseIcon.model_validate(raw)
            except Exception:  # noqa: BLE001
                return DEFAULT_ICON
        return DEFAULT_ICON
