"""图示 Agent（《项目文档》§8.4.1 Render-and-Review；《技术文档》§4.7）。

流程：生成 SVG → 清洗 → 渲染成 PNG → 读图自检（档案支持读图时）或启发式检查 →
不通过就带问题清单重画，默认最多 3 轮，仍不通过则降级为文字说明。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.errors import ConfigError
from ..infra.asset_store import save_versioned
from ..infra.svg_renderer import render_svg_png
from ..infra.svg_sanitizer import SanitizeResult, clean, is_acceptable

DEFAULT_MAX_ROUNDS = 3
CHECKLIST = (
    "1. 文字是否溢出画布或互相重叠？\n"
    "2. 图例 / 标注是否齐全，能否不看正文就理解？\n"
    "3. 颜色在明暗主题下是否都可读？\n"
    "4. 是否符合给定的知识点描述，是否存在误导性简化？\n"
    "只回答 PASS 或 FAIL，并列出问题（每行一条）。"
)


@dataclass
class SvgSpec:
    concept: str
    title: str = "示意图"
    style: str = "扁平、少量配色、带中文标注"
    caption_md: str = ""
    lesson_id: str = ""
    width: int = 820


@dataclass
class Verdict:
    passed: bool
    issues: list[str] = field(default_factory=list)

    @staticmethod
    def from_text(text: str) -> "Verdict":
        upper = (text or "").upper()
        issues = [line.strip("-• ").strip() for line in (text or "").splitlines() if line.strip()][1:]
        passed = "PASS" in upper and "FAIL" not in upper
        return Verdict(passed=passed, issues=[] if passed else (issues or ["自检未通过"]))


@dataclass
class SvgResult:
    ok: bool
    svg: str = ""
    png: bytes = b""
    rounds: int = 0
    fallback_md: str = ""
    issues: list[str] = field(default_factory=list)


def build_svg_prompt(spec: SvgSpec, feedback: list[str], previous: str = "") -> str:
    lines = [
        f"请画一张示意图：{spec.concept}",
        f"标题：{spec.title}；视觉风格：{spec.style}",
        f"画布宽度约 {spec.width}px。",
        "硬性要求：单个 <svg> 带 viewBox；不要 <script>、不要 <foreignObject>、不要任何外链；"
        "文字用中文，字号不小于 14；颜色用 currentColor 或主题色；"
        "元素数 < 400、体积 < 40KB；必须包含 <title> 与 <desc>。",
        "只输出 SVG 源码，不要解释、不要代码围栏。",
    ]
    if previous:
        lines.append("\n【当前版本（请只做必要修改）】\n" + previous[:6000])
    if feedback:
        lines.append("\n【需要修掉的问题】\n" + "\n".join(feedback))
    return "\n".join(lines)


class IllustratorAgent:
    name = "illustrator"

    def __init__(
        self,
        client: Any,
        *,
        renderer: Callable[..., bytes] = render_svg_png,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        vision: Callable[[bytes, str], str] | None = None,
    ) -> None:
        self.client = client
        self.renderer = renderer
        self.max_rounds = max_rounds
        self.vision = vision

    # --- 生成 -----------------------------------------------------------
    async def make_svg(self, spec: SvgSpec) -> SvgResult:
        feedback: list[str] = []
        last_svg = ""
        for round_no in range(1, self.max_rounds + 1):
            prompt = build_svg_prompt(spec, feedback, previous=last_svg)
            raw = await self._complete(prompt)
            candidate: SanitizeResult = clean(raw)
            if not is_acceptable(candidate):
                feedback = candidate.problems or ["SVG 不合格"]
                continue

            png = self.renderer(candidate.svg, width=spec.width) if self.renderer else b""
            verdict = await self._review(png, candidate.svg, spec)
            if verdict.passed:
                return SvgResult(ok=True, svg=candidate.svg, png=png, rounds=round_no)
            last_svg = candidate.svg
            feedback = verdict.issues or ["自检未通过"]

        return SvgResult(
            ok=False,
            svg=last_svg,
            rounds=self.max_rounds,
            fallback_md=spec.caption_md or f"（示意图「{spec.title}」未通过自检，已降级为文字说明。）",
            issues=feedback,
        )

    async def _complete(self, prompt: str) -> str:
        parts: list[str] = []
        async for piece in self.client.stream_chat([{"role": "user", "content": prompt}], task="illustrate"):
            parts.append(piece)
        return "".join(parts)

    async def _review(self, png: bytes, svg: str, spec: SvgSpec) -> Verdict:
        """默认走读图自检；没有视觉能力时退化到启发式检查（T-26b）。"""
        if self.vision is not None and png:
            try:
                text = self.vision(png, f"图示概念：{spec.concept}\n{CHECKLIST}")
                return Verdict.from_text(text)
            except Exception:  # noqa: BLE001 - 读图失败退回启发式
                pass
        return Verdict.from_text(self.heuristics(png, svg))

    @staticmethod
    def heuristics(png: bytes, svg: str) -> str:
        issues: list[str] = []
        if not png:
            issues.append("渲染失败：无法生成 PNG")
        if not re.search(r"<(?:\w+:)?title[\s>]", svg):
            issues.append("缺少 <title>")
        if not re.search(r"<(?:\w+:)?desc[\s>]", svg):
            issues.append("缺少 <desc>")
        text_nodes = re.findall(r"<text[^>]*>([^<]{0,80})", svg)
        if any(len(node) > 40 for node in text_nodes):
            issues.append("存在过长文本（可能溢出）")
        if issues:
            return "FAIL\n" + "\n".join(issues)
        return "PASS"

    # --- 落盘 -----------------------------------------------------------
    @staticmethod
    def save_asset(lesson_dir: str | Path, spec: SvgSpec, result: SvgResult, *, name: str = "diagram") -> dict[str, str]:
        """把当前版本写入 ``widgets/``；历史版本用 ``.vK.svg`` 命名（§3.3 / C-08）。"""
        payloads: dict[str, str | bytes] = {"svg": result.svg}
        if result.png:
            payloads["png"] = result.png
        return save_versioned(Path(lesson_dir) / "widgets", f"{name}-1", payloads)


class AnimatorAgent:
    """HTML 微动画（《项目文档》§8.4.2）：生成 → 清洗/CSP 包裹 → 5s 熔断降级。"""

    name = "animator"

    def __init__(self, client: Any, *, max_bytes: int = 80 * 1024) -> None:
        self.client = client
        self.max_bytes = max_bytes

    async def make_animation(self, concept: str, *, theme: dict[str, str] | None = None) -> dict[str, Any]:
        from ..infra.sandbox import prepare_animation_html, sanitize_animation_html

        prompt = (
            f"用单文件 HTML + 内联 CSS/JS 做一个演示「{concept}」的微型动画：\n"
            "- 宽高自适应；至少一个可交互控件（按钮或滑块）；\n"
            "- 不要任何外部资源、不要 fetch/XHR；\n"
            "- 支持 prefers-reduced-motion；体积 < 80KB；\n"
            "只输出 HTML 源码，不要解释。"
        )
        parts: list[str] = []
        async for piece in self.client.stream_chat([{"role": "user", "content": prompt}], task="illustrate"):
            parts.append(piece)
        raw = "".join(parts)
        _, problems = sanitize_animation_html(raw)
        if len(raw.encode("utf-8")) > self.max_bytes:
            return {"error": "动画体积超限", "fallback_md": f"（动画「{concept}」体积超限，已降级为文字说明。）"}
        return {"html": prepare_animation_html(raw, theme), "problems": problems}
