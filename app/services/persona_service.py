"""人设服务（《项目文档》§4.14；《技术文档》§13.9）。

职责：加载内置 + 用户人设、优先级合并（全局 → 课程 → 临时）、导入导出、试讲、
以及在索引库里登记人设（§3.7 的 ``personas`` 表）。
"""

from __future__ import annotations

from pathlib import Path

from ..agents.persona import (DEFAULT_PERSONA_ID, Persona, PersonaFormatError, dump_persona, load_persona_file, parse_persona)
from ..core.errors import ConfigError

BUILTIN_DIR = Path(__file__).resolve().parents[2] / "prompts" / "personas"


class PersonaService:
    def __init__(
        self,
        *,
        user_dir: str | Path,
        builtin_dir: str | Path | None = None,
        index: object | None = None,
    ) -> None:
        self.user_dir = Path(user_dir)
        self.builtin_dir = Path(builtin_dir) if builtin_dir is not None else BUILTIN_DIR
        self.index = index

    # --- 加载 -----------------------------------------------------------
    def list_personas(self) -> list[Persona]:
        """内置人设 + 用户人设（同 id 时用户覆盖内置，便于自定义）。"""
        merged: dict[str, Persona] = {}
        for directory, builtin in ((self.builtin_dir, True), (self.user_dir, False)):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                try:
                    persona = load_persona_file(path)
                except PersonaFormatError:
                    continue
                merged[persona.id] = Persona(**{**persona.__dict__, "builtin": builtin})
        return sorted(merged.values(), key=lambda item: (not item.builtin, item.name))

    def get(self, persona_id: str | None) -> Persona:
        """取人设；找不到或解析失败时回退默认人设（不阻塞学习，AC-35）。"""
        wanted = persona_id or DEFAULT_PERSONA_ID
        for persona in self.list_personas():
            if persona.id == wanted:
                return persona
        for persona in self.list_personas():
            if persona.id == DEFAULT_PERSONA_ID:
                return persona
        return Persona(id=DEFAULT_PERSONA_ID, name="默认讲师", body="", builtin=True)

    def resolve(
        self,
        *,
        global_default: str | None = None,
        course_persona: str | None = None,
        temporary: str | None = None,
    ) -> Persona:
        """优先级：临时 > 课程 > 全局（§10.6 优先级与冲突处理）。"""
        for candidate in (temporary, course_persona, global_default):
            if candidate:
                persona = self.get(candidate)
                if persona.id == candidate or persona_id_exists(self, candidate):
                    return persona
        return self.get(global_default or DEFAULT_PERSONA_ID)

    # --- 校验 / 导入 / 导出 ---------------------------------------------
    @staticmethod
    def validate_text(text: str, *, source: str = "") -> list[str]:
        """语法检查：返回可展示的问题列表（空列表代表通过）。"""
        try:
            parse_persona(text, source=source)
        except PersonaFormatError as exc:
            return [str(exc)]
        return []

    def import_persona(self, source_path: str | Path) -> Persona:
        """导入 ``.md`` 人设文件到用户人设目录（同名直接覆盖并保留旧版本号+1）。"""
        source = Path(source_path)
        persona = load_persona_file(source)
        self.user_dir.mkdir(parents=True, exist_ok=True)
        existing = self.user_dir / f"{persona.id}.md"
        if existing.exists():
            old = load_persona_file(existing)
            persona = Persona(**{**persona.__dict__, "version": max(old.version + 1, persona.version)})
        target = self.user_dir / f"{persona.id}.md"
        target.write_text(dump_persona(persona), encoding="utf-8", newline="\n")
        self.register(persona)
        return persona

    def export_persona(self, persona_id: str, target_path: str | Path) -> Path:
        persona = self.get(persona_id)
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_persona(persona), encoding="utf-8", newline="\n")
        return target

    def create_persona(self, persona: Persona) -> Persona:
        self.user_dir.mkdir(parents=True, exist_ok=True)
        target = self.user_dir / f"{persona.id}.md"
        target.write_text(dump_persona(persona), encoding="utf-8", newline="\n")
        self.register(persona)
        return persona

    def delete_persona(self, persona_id: str) -> bool:
        target = self.user_dir / f"{persona_id}.md"
        if target.exists():
            target.unlink()
            return True
        return False

    # --- 索引登记 -------------------------------------------------------
    def register(self, persona: Persona) -> None:
        if self.index is None:
            return
        self.index.upsert_persona(
            persona_id=persona.id,
            name=persona.name,
            builtin=persona.builtin,
            path=str(persona.path or ""),
            version=persona.version,
        )

    def register_all(self) -> int:
        count = 0
        for persona in self.list_personas():
            self.register(persona)
            count += 1
        return count

    def allowed_tools(self, persona: Persona, system_tools: list[str]) -> list[str]:
        """人设工具白名单与系统允许范围取交集（§10.6）。"""
        if not persona.tools:
            return list(system_tools)
        return [name for name in system_tools if name in set(persona.tools)]


def persona_id_exists(service: PersonaService, persona_id: str) -> bool:
    return any(item.id == persona_id for item in service.list_personas())


def ensure_persona(service: PersonaService, persona_id: str) -> Persona:
    persona = service.get(persona_id)
    if persona.id != persona_id:
        raise ConfigError(f"找不到人设：{persona_id}")
    return persona
