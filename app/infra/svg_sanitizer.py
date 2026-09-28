"""SVG 白名单清洗（《技术文档》§4.7 / §6；《项目文档》§8.4.1 与 T-16）。

硬性规范：单个 ``<svg>`` 带 ``viewBox``；禁 ``script`` / ``foreignObject`` / ``on*`` /
外链；体积 ≤ 40KB、元素数 ≤ 400；建议带 ``title`` / ``desc``。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field

MAX_BYTES = 40 * 1024
MAX_ELEMENTS = 400
FORBIDDEN_TAGS = {"script", "foreignobject", "iframe", "object", "embed", "use", "image"}
ALLOWED_URL_ATTRS = {"href", "xlink:href"}
EVENT_ATTR_RE = re.compile(r"^on[a-z]+$", re.IGNORECASE)
REMOTE_RE = re.compile(r"^\s*(?:https?:)?//", re.IGNORECASE)


@dataclass
class SanitizeResult:
    svg: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.svg)


def _local_name(tag: str) -> str:
    return tag.split("}")[-1].lower()


def clean(svg_text: str, *, max_bytes: int = MAX_BYTES, max_elements: int = MAX_ELEMENTS) -> SanitizeResult:
    """清洗 SVG；返回（干净 SVG, 问题清单）。问题清单非空即视为不合格。"""
    raw = (svg_text or "").strip()
    problems: list[str] = []
    if not raw:
        return SanitizeResult(problems=["SVG 内容为空"])
    if len(raw.encode("utf-8")) > max_bytes:
        problems.append(f"SVG 体积超过 {max_bytes // 1024}KB")

    fenced = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    start = fenced.find("<svg")
    if start < 0:
        return SanitizeResult(problems=["找不到 <svg> 根元素"])
    end = fenced.rfind("</svg>")
    snippet = fenced[start : end + 6] if end > start else fenced[start:]

    try:
        root = ElementTree.fromstring(snippet)
    except ElementTree.ParseError as exc:
        return SanitizeResult(problems=[f"SVG 不是合法 XML：{exc}"])

    if _local_name(root.tag) != "svg":
        problems.append("根元素不是 <svg>")
    if not root.attrib.get("viewBox"):
        problems.append("缺少 viewBox（无法自适应缩放）")

    # 先真正删除禁止元素（安全修复：只记问题不删等于没清洗）
    for parent in list(root.iter()):
        for child in list(parent):
            if _local_name(child.tag) in FORBIDDEN_TAGS:
                problems.append(f"已移除禁止元素：{_local_name(child.tag)}")
                parent.remove(child)

    has_title = False
    has_desc = False
    count = 0
    for element in root.iter():
        count += 1
        name = _local_name(element.tag)
        if name == "title":
            has_title = True
        if name == "desc":
            has_desc = True

        for attribute in list(element.attrib):
            attr_name = _local_name(attribute)
            value = element.attrib[attribute]
            if EVENT_ATTR_RE.match(attr_name):
                problems.append(f"包含事件属性：{attr_name}")
                element.attrib.pop(attribute, None)
                continue
            if attr_name in ALLOWED_URL_ATTRS and REMOTE_RE.match(str(value)):
                problems.append("包含外链引用")
                element.attrib.pop(attribute, None)
            elif isinstance(value, str) and "url(http" in value.lower():
                problems.append("包含远程 url()")
                element.attrib.pop(attribute, None)
    if count > max_elements:
        problems.append(f"元素数超过 {max_elements}")
    if not has_title:
        problems.append("缺少 <title>（无障碍要求）")
    if not has_desc:
        problems.append("缺少 <desc>（无障碍要求）")

    cleaned = ElementTree.tostring(root, encoding="unicode")
    return SanitizeResult(svg=cleaned, problems=problems)


# 只要出现这些就判为「不可用」；被清洗掉的危险片段只记问题、不算失败（§4.7：清洗后即可用）
BLOCKING_TOKENS = ("不是合法 XML", "找不到", "根元素", "体积超过", "元素数超过", "内容为空", "viewBox")


def is_acceptable(result: SanitizeResult) -> bool:
    """缺 title/desc 只是提示；缺 viewBox、体积/元素数超限、非法 XML 才算不合格。"""
    return bool(result.svg) and not any(
        token in problem for problem in result.problems for token in BLOCKING_TOKENS
    )
