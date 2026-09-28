"""人设模型与解析（《项目文档》§10.6、§4.14；《技术文档》§13.9）。

人设文件格式：``personas/<id>.md`` = YAML 头部 + Markdown 正文。
本模块只做**解析与校验**（含错误行号），优先级合并与导入导出在 ``services/persona_service.py``。
为了让文件可由用户手写，这里只支持 YAML 的一个安全子集（标量、内联列表、布尔、整数），
不引入 PyYAML 依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core.errors import AtmError

PERSONA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
LIST_RE = re.compile(r"^\[(.*)\]$")
SAFE_KEYS = {"id", "name", "avatar", "tone", "allow_roleplay", "tools", "version", "based_on"}


class PersonaFormatError(AtmError):
    """人设文件语法错误（带行号，见 §10.6「报错定位到行号」）。"""

    user_message = "人设文件格式有问题"

    def __init__(self, message: str, *, line: int | None = None, path: str | None = None) -> None:
        location = ""
        if path:
            location += f"{path}"
        if line:
            location += f" 第 {line} 行"
        super().__init__(f"{message}（{location}）" if location else message)
        self.line = line
        self.path = path


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    body: str
    avatar: str = ""
    tone: tuple[str, ...] = ()
    allow_roleplay: bool = False
    tools: tuple[str, ...] = ()
    version: int = 1
    based_on: str | None = None
    path: Path | None = None
    builtin: bool = False
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def effective_body(self) -> str:
        """``allow_roleplay = false`` 时忽略角色扮演设定，只保留讲课习惯。"""
        if self.allow_roleplay:
            return self.body
        lines = [
            line
            for line in self.body.splitlines()
            if not any(token in line for token in ("角色", "自称", "扮演", "你是"))
        ]
        return "\n".join(lines).strip()


def _parse_scalar(raw: str) -> object:
    text = raw.strip()
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if text.isdigit():
        return int(text)
    return text.strip("'\"")


def parse_persona(text: str, *, source: str = "") -> Persona:
    """解析人设文本；语法错误抛 ``PersonaFormatError``（含行号）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PersonaFormatError("人设文件必须以 --- 开头的 YAML 头部开始", line=1, path=source)

    meta: dict[str, object] = {}
    body_start = None
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            body_start = index + 1
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise PersonaFormatError("YAML 头部的每一行都要写成 key: value", line=index + 1, path=source)
        key, _, raw_value = line.partition(":")
        key = key.strip()
        if key not in SAFE_KEYS:
            raise PersonaFormatError(f"不支持的字段：{key}", line=index + 1, path=source)
        meta[key] = _parse_scalar(raw_value)
    if body_start is None:
        raise PersonaFormatError("YAML 头部没有结束标记 ---", path=source)

    persona_id = str(meta.get("id") or "").strip()
    if not persona_id:
        raise PersonaFormatError("缺少必填字段 id", line=2, path=source)
    if not PERSONA_ID_RE.match(persona_id):
        raise PersonaFormatError("id 只能用小写字母、数字与连字符", line=2, path=source)

    name = str(meta.get("name") or "").strip()
    if not name:
        raise PersonaFormatError("缺少必填字段 name", line=2, path=source)

    version = meta.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise PersonaFormatError("version 必须是 ≥1 的整数", path=source)

    tone = meta.get("tone") or []
    tools = meta.get("tools") or []
    issues: list[str] = []
    if meta.get("allow_roleplay") not in (True, False, None, ""):
        issues.append("allow_roleplay 只能是 true / false，已按 false 处理")

    return Persona(
        id=persona_id,
        name=name,
        body="\n".join(lines[body_start:]).strip(),
        avatar=str(meta.get("avatar") or ""),
        tone=tuple(tone) if isinstance(tone, list) else (str(tone),),
        allow_roleplay=bool(meta.get("allow_roleplay") is True),
        tools=tuple(tools) if isinstance(tools, list) else (str(tools),),
        version=int(version),
        based_on=str(meta["based_on"]) if meta.get("based_on") else None,
        path=Path(source) if source else None,
        issues=tuple(issues),
    )


def load_persona_file(path: str | Path) -> Persona:
    target = Path(path)
    return parse_persona(target.read_text(encoding="utf-8-sig"), source=str(target))


def dump_persona(persona: Persona) -> str:
    """导出成人设文件文本（导入导出的往返格式，§4.14 AC-48）。"""
    tone = ", ".join(persona.tone)
    tools = ", ".join(persona.tools)
    header = [
        "---",
        f"id: {persona.id}",
        f"name: {persona.name}",
        f"avatar: {persona.avatar}",
        f"tone: [{tone}]",
        f"allow_roleplay: {'true' if persona.allow_roleplay else 'false'}",
        f"tools: [{tools}]",
        f"version: {persona.version}",
    ]
    if persona.based_on:
        header.append(f"based_on: {persona.based_on}")
    header.append("---")
    return "\n".join([*header, "", persona.body.strip(), ""])


DEFAULT_PERSONA_ID = "default-teacher"
