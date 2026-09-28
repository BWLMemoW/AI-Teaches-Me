"""资料服务（《项目文档》FR-03 / FR-08；《技术文档》§3.3、§3.7）。

职责：把用户上传的资料落盘到 ``materials/raw/``，解析结果写 ``materials/parsed/``，
清单写 ``materials/materials.json``，分块索引进 SQLite FTS5（trigram）。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..infra import file_repository
from ..infra.material_parser import ParsedMaterial, checksum_of, material_id_for, parse_material
from ..infra.sqlite_index import SqliteIndex


@dataclass
class MaterialImportResult:
    course_id: str
    materials: list[ParsedMaterial] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)

    @property
    def parsed(self) -> list[ParsedMaterial]:
        return [item for item in self.materials if item.status == "parsed"]

    @property
    def failed(self) -> list[ParsedMaterial]:
        return [item for item in self.materials if item.status not in ("parsed",)]

    def summary_for_director(self, *, limit: int = 4000) -> str:
        """拼成给总监 Agent 的资料摘要（带 ``material_id#page`` 定位）。"""
        lines: list[str] = []
        for item in self.parsed:
            lines.append(f"[{item.material_id}] {item.filename}（{item.kind}，{item.pages} 页）")
            for chunk in item.chunks[:4]:
                lines.append(f"  - {item.material_id}#p{chunk['page']}：{chunk['text'][:200]}")
        return "\n".join(lines)[:limit]


class MaterialService:
    def __init__(self, *, course_dir: str | Path, index: SqliteIndex | None = None) -> None:
        self.course_dir = Path(course_dir)
        self.index = index

    @property
    def raw_dir(self) -> Path:
        return self.course_dir / "materials" / "raw"

    @property
    def parsed_dir(self) -> Path:
        return self.course_dir / "materials" / "parsed"

    @property
    def manifest_path(self) -> Path:
        return self.course_dir / "materials" / "materials.json"

    def manifest(self) -> dict:
        return file_repository.read_json_or_default(self.manifest_path, {"materials": []}) or {"materials": []}

    def import_files(self, files: Sequence[str | Path], *, course_id: str = "", move: bool = False) -> MaterialImportResult:
        """导入并解析资料（同步；调用方应放在工作线程里）。"""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        result = MaterialImportResult(course_id=course_id)
        manifest = self.manifest()
        entries = list(manifest.get("materials") or [])

        for source in files:
            path = Path(source)
            target = self.raw_dir / path.name
            if path.resolve() != target.resolve():
                if move:
                    shutil.move(str(path), str(target))
                else:
                    shutil.copy2(path, target)
            identifier = material_id_for(target)
            parsed = parse_material(target, material_id=identifier)
            result.materials.append(parsed)
            if parsed.status == "parsed":
                parsed_file = self.parsed_dir / f"{target.stem}.md"
                file_repository.write_text(parsed_file, parsed.markdown)
                result.files.append(parsed_file)
                if self.index is not None:
                    self.index.upsert_material(
                        material_id=identifier,
                        course_id=course_id or self.course_dir.name,
                        filename=target.name,
                        kind=parsed.kind,
                        pages=parsed.pages,
                        status="parsed",
                        checksum=checksum_of(target),
                    )
                    self.index.index_chunks(identifier, parsed.chunks)
            entries.append(
                {
                    "id": identifier,
                    "filename": target.name,
                    "kind": parsed.kind,
                    "pages": parsed.pages,
                    "status": parsed.status,
                    "error": parsed.error,
                    "parsed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "checksum": checksum_of(target),
                }
            )

        file_repository.write_json(self.manifest_path, {"materials": entries})
        return result

    def materials(self) -> list[dict]:
        return list(self.manifest().get("materials") or [])

    def search(self, query: str, *, k: int = 5, material_ids: Iterable[str] | None = None) -> list[dict]:
        if self.index is None:
            return []
        return self.index.search_chunks(query, k=k, material_ids=list(material_ids) if material_ids else None)

    def slices_for_knowledge_points(self, knowledge_points: Sequence[dict], *, k: int = 2) -> list[dict]:
        """讲义生成前的确定性预取（§4.5：系统预取负责「确定要查的」）。"""
        snippets: list[dict] = []
        for point in knowledge_points:
            query = str(point.get("title") or "")
            if not query:
                continue
            snippets.extend(self.search(query, k=k))
        return snippets[: k * 3]
