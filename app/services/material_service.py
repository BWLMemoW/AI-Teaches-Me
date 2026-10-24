"""资料服务（《项目文档》FR-03 / FR-08；《技术文档》§3.3、§3.7）。

职责：把用户上传的资料落盘到 ``materials/raw/``，解析结果写 ``materials/parsed/``，
清单写 ``materials/materials.json``，分块索引进 SQLite FTS5（trigram）。

这一版新增的三件事：

1. **候选知识图的统一处理**：本地过滤后的图在这里做「超过阈值先确认 → 查缓存 → 调模型 →
   结果并回正文」；模型调用由调用方以 ``vision`` 回调注入，本层保持无 Qt、无 SDK；
2. **可重建**：解析稿旁边存一份 ``.chunks.json``，索引库删掉后能按课程目录重建分块；
3. **按课程隔离**：检索一律可带 ``course_id``，跨课程资料不会串味。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..infra import file_repository
from ..infra.material_parser import (
    VISION_PROMPT,
    VISION_BATCH_PROMPT,
    MaterialParseOptions,
    ParsedMaterial,
    apply_vision,
    checksum_of,
    material_id_for,
    parse_material,
    split_batch_answer,
)
from ..infra.sqlite_index import SqliteIndex

#: 预计要识别的图超过这个数量就先问用户（避免一次导入几百次模型调用）
VISION_CONFIRM_THRESHOLD = 30
#: 一次调用里打包几张图（扫描版 PDF 几百页时，调用量按这个比例下降）
VISION_BATCH_SIZE = 4
#: 同时跑几批识图。串行时一批要等 10–30 秒，几批叠起来就像卡死；
#: 3 路并发能在不增加调用费用的前提下明显缩短总等待时间。
VISION_PARALLEL = 3
#: 识图缓存目录名（放在工作区 ``data/cache/`` 下）
VISION_CACHE_DIR = "material_vision"
VISION_CACHE_VERSION = 1
#: 送模型前统一压缩：长边上限与 JPEG 质量（控制体积与费用）
VISION_MAX_EDGE = 1600
VISION_JPEG_QUALITY = 85
VISION_MAX_BYTES = 6 * 1024 * 1024

LOGGER = logging.getLogger(__name__)


def _cancelled(token: Any) -> bool:
    return bool(token is not None and getattr(token, "cancelled", False))


def _emit(callback: Callable[[dict], None] | None, payload: dict) -> None:
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:  # noqa: BLE001 - 进度回调失败不能影响导入
        pass


def prepare_vision_image(data: bytes) -> bytes:
    """把图片压成适合发送的 JPEG（长边 ≤1600、质量 85）；失败就原样返回。"""
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            converted = image.convert("RGB")
            longest = max(converted.width, converted.height)
            if longest > VISION_MAX_EDGE:
                scale = VISION_MAX_EDGE / float(longest)
                converted = converted.resize(
                    (max(1, int(converted.width * scale)), max(1, int(converted.height * scale)))
                )
            buffer = BytesIO()
            converted.save(buffer, format="JPEG", quality=VISION_JPEG_QUALITY, optimize=True)
            encoded = buffer.getvalue()
        return encoded if 0 < len(encoded) <= VISION_MAX_BYTES else data
    except Exception:  # noqa: BLE001 - 压缩失败就用原图
        return data


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
        """给总监 Agent 的资料摘要——分级摘要的本地部分（带 ``material_id#page`` 定位）。"""
        return build_material_digest(self.parsed, budget_chars=limit)

    @property
    def report(self) -> list[dict]:
        """资料体检报告（每份文件一行）。"""
        return [item.report() for item in self.materials]


class MaterialService:
    def __init__(
        self,
        *,
        course_dir: str | Path,
        index: SqliteIndex | None = None,
        cache_dir: str | Path | None = None,
        vision: Callable[[bytes, str], str] | None = None,
        vision_batch: Callable[[list[bytes], str], str] | None = None,
        cancel: Any = None,
        on_event: Callable[[dict], None] | None = None,
        confirm: Callable[[int, list[str]], bool] | None = None,
        include_all_images: bool = False,
    ) -> None:
        self.course_dir = Path(course_dir)
        self.index = index
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.vision = vision
        self.vision_batch = vision_batch
        self.cancel = cancel
        self.on_event = on_event
        self.confirm = confirm
        self.include_all_images = include_all_images
        self._vision_results: dict[int, dict[str, Any]] = {}

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

    # --- 解析与识图 -----------------------------------------------------
    def parse_files(self, files: Sequence[str | Path], *, course_id: str = "") -> list[ParsedMaterial]:
        """解析（含候选知识图识别）但不落盘：给「建课前预读」与「正式导入」共用。"""
        parsed_items: list[ParsedMaterial] = []
        # 最多两轮：用户在确认框里勾了「连装饰图一起识别」时，带全图重解析一次
        for _attempt in range(2):
            parsed_items = self._parse_only(files, course_id=course_id)
            if not self._apply_vision(parsed_items):
                return parsed_items
        return parsed_items

    def _parse_only(self, files: Sequence[str | Path], *, course_id: str = "") -> list[ParsedMaterial]:
        parsed_items: list[ParsedMaterial] = []
        file_list = [Path(item) for item in files]
        for index, path in enumerate(file_list, start=1):
            if _cancelled(self.cancel):
                break
            _emit(self.on_event, {"stage": "scan", "file": path.name, "index": index, "total": len(file_list)})
            checksum = checksum_of(path) if path.exists() else ""
            parsed = parse_material(
                path,
                material_id=material_id_for(path, course_id=course_id, checksum=checksum),
                options=MaterialParseOptions(include_all_images=self.include_all_images),
            )
            parsed.checksum = checksum
            parsed_items.append(parsed)
        return parsed_items

    def _apply_vision(self, materials: Sequence[ParsedMaterial]) -> bool:
        """本地过滤后剩下的候选知识图：确认 → 缓存 → 模型 → 并回正文。

        返回 ``True`` 表示用户要求「连装饰图一起识别」，调用方需要带全图重新解析一轮。
        """
        candidates = [(item, image) for item in materials for image in item.images]
        if not candidates:
            return False
        if self.vision is None and self.vision_batch is None:
            for item in materials:
                item.vision_skipped = bool(item.images)
            return False
        if _cancelled(self.cancel):
            for item in materials:
                item.vision_skipped = bool(item.images)
            return False
        # 只把「还没缓存过」的图算进费用：分批上传时第二次不该再问一遍
        uncached = [(item, image) for item, image in candidates if self._read_vision_cache(image.data) is None]
        if len(uncached) > VISION_CONFIRM_THRESHOLD and self.confirm is not None:
            names = sorted({item.filename for item, _image in uncached})
            answer = self.confirm(len(uncached), names)
            proceed, include_all = True, False
            if isinstance(answer, dict):
                proceed = bool(answer.get("proceed"))
                include_all = bool(answer.get("include_all_images"))
            elif isinstance(answer, (tuple, list)) and len(answer) == 2:
                proceed, include_all = bool(answer[0]), bool(answer[1])
            else:
                proceed = bool(answer)
            if not proceed:
                for item in materials:
                    item.vision_skipped = bool(item.images)
                return False
            if include_all and not self.include_all_images:
                self.include_all_images = True
                return True
        _emit(self.on_event, {"stage": "confirm", "images": len(candidates)})
        batches = self._batches(candidates)
        consecutive_failures = 0
        done = 0
        abort = threading.Event()

        def run_batch(batch: list[tuple[ParsedMaterial, Any]]) -> list[str | None]:
            """一批图片：先打包读，格式不对再逐张补；取消时立刻停下。"""
            if _cancelled(self.cancel) or abort.is_set():
                return [None] * len(batch)
            texts = self._recognize_batch([image.data for _item, image in batch])
            if texts is None and not _cancelled(self.cancel) and not abort.is_set():
                # 打包失败 / 模型没按格式回答 → 退回逐张，保证不漏页
                texts = []
                for _item, image in batch:
                    if _cancelled(self.cancel) or abort.is_set():
                        texts.append(None)
                        continue
                    texts.append(self._recognize_single(image.data))
            return texts or [None] * len(batch)

        def record(batch: list[tuple[ParsedMaterial, Any]], texts: Sequence[str | None]) -> None:
            nonlocal consecutive_failures, done
            for (item, image), text in zip(batch, texts):
                if text is not None:
                    item.vision_called += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                self._vision_results.setdefault(id(item), {})[image.key] = text
            done += len(batch)
            _emit(
                self.on_event,
                {
                    "stage": "vision",
                    "file": batch[0][0].filename,
                    "index": min(done, len(candidates)),
                    "total": len(candidates),
                },
            )
            # 连续失败（没配 Key / 端点不支持读图）就别硬撑：剩下的按「未识别」处理
            if consecutive_failures >= 3:
                abort.set()

        workers = max(1, min(int(VISION_PARALLEL), len(batches)))
        if workers == 1:
            for batch in batches:
                record(batch, run_batch(batch))
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="atm-vision") as pool:
                futures = {pool.submit(run_batch, batch): batch for batch in batches}
                for future in as_completed(futures):
                    batch = futures[future]
                    try:
                        texts = future.result()
                    except Exception as exc:  # noqa: BLE001 - 单批异常不该毁掉整次导入
                        LOGGER.warning("识图批次失败（%s）：%s", batch[0][0].filename, exc)
                        texts = [None] * len(batch)
                    record(batch, texts)
                    if _cancelled(self.cancel) or abort.is_set():
                        for pending in futures:
                            pending.cancel()

        if _cancelled(self.cancel) or abort.is_set():
            for item, image in candidates:
                if self._vision_results.get(id(item), {}).get(image.key) is None:
                    item.vision_skipped = True
        for item in materials:
            results = self._vision_results.pop(id(item), None)
            if results is not None:
                apply_vision(item, results)
        return False

    @staticmethod
    def _batches(candidates: Sequence[tuple[ParsedMaterial, Any]]) -> list[list[tuple[ParsedMaterial, Any]]]:
        """同一份资料内按顺序打包（跨资料不混批，避免页码对不上）。"""
        batches: list[list[tuple[ParsedMaterial, Any]]] = []
        for item, image in candidates:
            if batches and batches[-1][0][0] is item and len(batches[-1]) < VISION_BATCH_SIZE:
                batches[-1].append((item, image))
            else:
                batches.append([(item, image)])
        return batches

    def _recognize_batch(self, images: Sequence[bytes]) -> list[str | None] | None:
        """打包识别：全部命中缓存就直接返回；拿不到合规回答返回 ``None``（调用方逐张重试）。"""
        cached = [self._read_vision_cache(data) for data in images]
        if all(item is not None for item in cached):
            return cached
        if self.vision_batch is None:
            return None
        prompt = VISION_BATCH_PROMPT.format(count=len(images))
        try:
            answer = self.vision_batch([prepare_vision_image(data) for data in images], prompt)
        except Exception as exc:  # noqa: BLE001 - 打包失败就退回逐张
            LOGGER.warning("打包识图失败，改逐张重试（%d 张）：%s", len(images), exc)
            return None
        parts = split_batch_answer(str(answer or ""), len(images))
        if parts is None:
            LOGGER.warning("打包识图回答缺少「【第N张】」编号，改逐张重试（%d 张）", len(images))
            return None
        results: list[str | None] = []
        for data, part in zip(images, parts):
            text = part.strip()
            if not text:
                results.append(None)
                continue
            self._write_vision_cache(data, text)
            results.append(text)
        return results

    def _recognize(self, data: bytes) -> str | None:
        """单张图识别：命中缓存就不花钱；失败返回 ``None``（调用方记为失败）。"""
        cached = self._read_vision_cache(data)
        if cached is not None:
            return cached
        if self.vision is None:
            return None
        try:
            text = self.vision(prepare_vision_image(data), VISION_PROMPT)
        except Exception:  # noqa: BLE001 - 单张失败不影响整份资料
            return None
        value = str(text or "").strip()
        if not value:
            return None
        self._write_vision_cache(data, value)
        return value

    def _recognize_single(self, data: bytes) -> str | None:
        """退回逐张时的统一入口：优先用单图回调，没有就把打包回调当单图用。"""
        if self.vision is not None:
            return self._recognize(data)
        cached = self._read_vision_cache(data)
        if cached is not None:
            return cached
        parts = self._recognize_batch([data])
        return parts[0] if parts else None

    # --- 识图缓存 -------------------------------------------------------
    @property
    def vision_cache_dir(self) -> Path | None:
        return (self.cache_dir / VISION_CACHE_DIR) if self.cache_dir is not None else None

    def _vision_cache_path(self, data: bytes) -> Path | None:
        base = self.vision_cache_dir
        if base is None:
            return None
        digest = hashlib.sha1(data).hexdigest()
        return base / f"{digest}-v{VISION_CACHE_VERSION}.json"

    def _read_vision_cache(self, data: bytes) -> str | None:
        path = self._vision_cache_path(data)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        text = str(payload.get("text") or "").strip()
        return text or None

    def _write_vision_cache(self, data: bytes, text: str) -> None:
        path = self._vision_cache_path(data)
        if path is None:
            return
        try:
            file_repository.write_json(path, {"version": VISION_CACHE_VERSION, "text": text})
        except OSError:  # 缓存写失败不影响导入
            pass

    def import_files(self, files: Sequence[str | Path], *, course_id: str = "", move: bool = False) -> MaterialImportResult:
        """导入并解析资料（同步；调用方应放在工作线程里）。"""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        result = MaterialImportResult(course_id=course_id)
        manifest = self.manifest()
        entries = list(manifest.get("materials") or [])

        file_list = [Path(item) for item in files]
        targets: list[Path] = []
        for path in file_list:
            target = self._raw_target_for(path)
            if path.resolve() != target.resolve():
                if move:
                    shutil.move(str(path), str(target))
                else:
                    shutil.copy2(path, target)
            targets.append(target)
        result.materials = self.parse_files(targets, course_id=course_id or self.course_dir.name)
        for parsed in result.materials:
            target = self.raw_dir / parsed.filename
            identifier = parsed.material_id
            if parsed.status == "parsed":
                parsed_file = self.parsed_dir / f"{target.stem}.md"
                file_repository.write_text(parsed_file, parsed.markdown)
                result.files.append(parsed_file)
                # 分块一并落盘：索引库是可重建缓存，靠这份 json 自愈（§3.7）
                file_repository.write_json(
                    self.parsed_dir / f"{target.stem}.chunks.json",
                    {"version": 1, "chunks": parsed.chunks},
                )
                # 逐页要点也一并落盘：讲义生成时读它做「全貌」，不必每次重新抽取
                file_repository.write_json(
                    self.parsed_dir / f"{target.stem}.digest.json",
                    {
                        "version": DIGEST_VERSION,
                        "checksum": parsed.checksum,
                        "items": build_page_digest(parsed.chunks),
                    },
                )
                if self.index is not None:
                    self.index.upsert_material(
                        material_id=identifier,
                        course_id=course_id or self.course_dir.name,
                        filename=target.name,
                        kind=parsed.kind,
                        pages=parsed.pages,
                        status="parsed",
                        checksum=parsed.checksum,
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
                    "source": "user",
                    "parsed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "checksum": parsed.checksum,
                    "report": parsed.report(),
                }
            )

        file_repository.write_json(self.manifest_path, {"materials": entries})
        return result

    def _raw_target_for(self, path: Path) -> Path:
        """决定原件在 ``materials/raw/`` 里的落点。

        分批上传时很容易出现「每章都叫 PPT.pptx」：同名且内容不同就自动加 ``-2``、``-3``
        后缀，避免后传的覆盖先传的；同名同内容则复用同一个文件（重复导入幂等）。
        """
        target = self.raw_dir / path.name
        if not target.exists():
            return target
        if target.resolve() == path.resolve():
            return target
        try:
            digest = checksum_of(path)
        except OSError:
            digest = ""
        counter = 2
        while target.exists():
            try:
                same = checksum_of(target) == digest
            except OSError:
                same = False
            if same:
                break
            target = self.raw_dir / f"{path.stem}-{counter}{path.suffix}"
            counter += 1
        return target

    def materials(self) -> list[dict]:
        return list(self.manifest().get("materials") or [])

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        material_ids: Iterable[str] | None = None,
        course_id: str | None = None,
    ) -> list[dict]:
        if self.index is None:
            return []
        return self.index.search_chunks(
            query,
            k=k,
            material_ids=list(material_ids) if material_ids else None,
            course_id=course_id,
        )

    def slices_for_knowledge_points(
        self, knowledge_points: Sequence[dict], *, k: int = 2, course_id: str | None = None
    ) -> list[dict]:
        """讲义生成前的确定性预取（§4.5：系统预取负责「确定要查的」）。"""
        snippets: list[dict] = []
        for point in knowledge_points:
            query = str(point.get("title") or "")
            if not query:
                continue
            snippets.extend(self.search(query, k=k, course_id=course_id))
        return snippets[: k * 3]

    # --- 重建（删库自愈） -----------------------------------------------
    @staticmethod
    def reindex_course(course_dir: str | Path, *, course_id: str, index: SqliteIndex) -> int:
        """按课程目录重建资料索引（``library.db`` 删掉之后的自愈路径）。"""
        directory = Path(course_dir)
        manifest_path = directory / "materials" / "materials.json"
        if not manifest_path.exists():
            return 0
        manifest = file_repository.read_json_or_default(manifest_path, {"materials": []}) or {"materials": []}
        parsed_dir = directory / "materials" / "parsed"
        raw_dir = directory / "materials" / "raw"
        restored = 0
        for entry in manifest.get("materials") or []:
            if not isinstance(entry, dict) or str(entry.get("source") or "") == "outline":
                continue
            material_id = str(entry.get("id") or "")
            filename = str(entry.get("filename") or "")
            if not material_id or not filename or index.count_chunks(material_id):
                continue
            chunks: list[dict] = []
            stored = parsed_dir / f"{Path(filename).stem}.chunks.json"
            if stored.exists():
                payload = file_repository.read_json_or_default(stored, {}) or {}
                chunks = [item for item in (payload.get("chunks") or []) if isinstance(item, dict)]
            if not chunks:
                raw = raw_dir / filename
                if not raw.exists():
                    continue
                reparsed = parse_material(raw, material_id=material_id, course_id=course_id)
                chunks = reparsed.chunks
                if reparsed.status == "parsed":
                    file_repository.write_text(parsed_dir / f"{Path(filename).stem}.md", reparsed.markdown)
                    file_repository.write_json(stored, {"version": 1, "chunks": chunks})
            if not chunks:
                continue
            index.upsert_material(
                material_id=material_id,
                course_id=course_id,
                filename=filename,
                kind=str(entry.get("kind") or ""),
                pages=int(entry.get("pages") or 0),
                status="parsed",
                checksum=str(entry.get("checksum") or ""),
            )
            index.index_chunks(material_id, chunks)
            restored += len(chunks)
        return restored


def build_material_digest(materials: Sequence[ParsedMaterial], *, budget_chars: int = 24000) -> str:
    """分级摘要的本地部分：目录结构 + 逐段要点 + 关键词表 + 每份至少一条摘要。

    以前这里是「前 4 块 × 200 字，再整体砍到 4000 字」——一本 300 页教材只能看到一页；
    现在按文档 §8.2 的分级摘要思路压缩，并保证**每份资料都有内容进上下文**。
    """
    parsed = [item for item in materials if item.status == "parsed"]
    if not parsed:
        return ""
    share = max(800, budget_chars // max(len(parsed), 1))
    sections = [_material_section(item, share) for item in parsed]
    return "\n\n".join(sections)[:budget_chars]


def _material_section(item: ParsedMaterial, share: int) -> str:
    lines = [
        f"### 资料 [{item.material_id}] {item.filename}"
        f"（{item.kind}，{item.pages} 页，{len(item.chunks)} 段）"
    ]
    if item.headings:
        lines.append("目录结构：" + " / ".join(item.headings[:12]))
    if item.has_vision_text:
        lines.append("（本资料含模型识图内容，已标注〔模型识图〕）")
    keywords = _keywords(item)
    if keywords:
        lines.append("关键词：" + "、".join(keywords))
    lines.append("要点：")
    per_item_budget = max(200, share // max(min(len(item.chunks), 24), 1))
    seen: set[str] = set()
    for chunk in item.chunks[:24]:
        text = _first_sentences(str(chunk.get("text") or ""), limit=per_item_budget)
        if not text or text in seen:
            continue
        seen.add(text)
        locator = str(chunk.get("locator") or f"#p{chunk.get('page') or 0}")
        source = str(chunk.get("source") or "text")
        tag = f"[{source}]" if source != "text" else ""
        lines.append(f"- {item.material_id}{locator}{tag}：{text}")
    return "\n".join(lines)


def _first_sentences(text: str, *, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    for mark in ("。", "；", "！", "？", ".", ";", "!", "?"):
        position = cut.rfind(mark)
        if position >= limit * 0.5:
            return cut[: position + 1]
    return cut + "…"


def _keywords(item: ParsedMaterial, *, top: int = 12) -> list[str]:
    """极简本地关键词：统计 2–6 字词块的词频（不引入分词依赖）。"""
    text = " ".join(str(chunk.get("text") or "") for chunk in item.chunks[:120])
    counts: dict[str, int] = {}
    for token in re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z][A-Za-z0-9\-]{2,20}", text):
        if token.isdigit():
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [token for token, count in ranked[:top] if count >= 2]


# --- 章节 / 小节指定参考（outline.json 的 source_refs） ----------------------
#: 一条引用的写法：``m-1a2b``（整份）｜ ``m-1a2b#p3`` ｜ ``m-1a2b#p3-8`` ｜ ``m-1a2b#s2-5``
_SOURCE_REF_RE = re.compile(
    r"^\s*(?P<material>[^#\s]+)\s*(?:#\s*(?P<kind>[A-Za-z]*)\s*(?P<start>\d+)\s*(?:-\s*(?P<end>\d+)\s*)?)?$"
)


def parse_source_ref(ref: str) -> dict | None:
    """把一条 ``source_refs`` 拆成 ``{material_id, kind, start, end}``；读不懂返回 ``None``。

    ``kind`` 只作记录（``p`` 页码 / ``s`` 幻灯片），匹配时按数字比较，写错也不至于整条失效。
    """
    match = _SOURCE_REF_RE.match(str(ref or ""))
    if match is None:
        return None
    kind = str(match.group("kind") or "").lower()
    if kind and kind not in ("p", "s"):
        return None
    start = match.group("start")
    parsed = {"material_id": match.group("material"), "kind": kind, "start": None, "end": None}
    if start is None:
        return parsed
    first = int(start)
    last = int(match.group("end")) if match.group("end") is not None else first
    if last < first:
        first, last = last, first
    parsed["start"], parsed["end"] = first, last
    return parsed


def format_source_ref(material_id: str, *, kind: str = "", start: int | None = None, end: int | None = None) -> str:
    """拼回一条 ``source_refs``（整份资料 / 单页 / 页码范围）。"""
    identifier = str(material_id or "").strip()
    if not identifier or start is None:
        return identifier
    letter = kind if kind in ("p", "s") else "p"
    if end is None or int(end) == int(start):
        return f"{identifier}#{letter}{int(start)}"
    return f"{identifier}#{letter}{min(int(start), int(end))}-{max(int(start), int(end))}"


def _chunk_number(chunk: dict) -> int | None:
    """取分块的页码 / 幻灯片号（``#p12`` / ``#s3``，读不到时回退 ``page``）。"""
    match = re.search(r"#(?:p|s)\s*(\d+)", str(chunk.get("locator") or ""))
    if match is not None:
        return int(match.group(1))
    try:
        value = int(chunk.get("page"))
    except (TypeError, ValueError):
        return None
    return value or None


def ref_matches_chunk(ref: dict, chunk: dict) -> bool:
    """这条引用是否覆盖该分块（没写范围 = 整份资料）。"""
    if ref.get("start") is None:
        return True
    number = _chunk_number(chunk)
    if number is None:
        return False
    return int(ref["start"]) <= number <= int(ref["end"])


def material_manifest_index(course_dir: str | Path) -> dict[str, dict]:
    """``materials.json`` → ``material_id -> 条目``（跳过解析失败与大纲派生的条目）。"""
    manifest_path = Path(course_dir) / "materials" / "materials.json"
    payload = file_repository.read_json_or_default(manifest_path, {"materials": []}) or {"materials": []}
    index: dict[str, dict] = {}
    for entry in payload.get("materials") or []:
        if not isinstance(entry, dict):
            continue
        identifier = str(entry.get("id") or "")
        if identifier and str(entry.get("status") or "parsed") == "parsed":
            index[identifier] = entry
    return index


def material_chunks(course_dir: str | Path, entry: dict) -> list[dict]:
    """读一份资料的解析分块（优先 ``.chunks.json``，缺失时回退整篇 md 一段）。"""
    parsed_dir = Path(course_dir) / "materials" / "parsed"
    stem = Path(str(entry.get("filename") or "")).stem
    if not stem:
        return []
    stored = parsed_dir / f"{stem}.chunks.json"
    if stored.exists():
        payload = file_repository.read_json_or_default(stored, {}) or {}
        chunks = [item for item in (payload.get("chunks") or []) if isinstance(item, dict)]
        if chunks:
            return chunks
    text_path = parsed_dir / f"{stem}.md"
    if text_path.exists():
        text = text_path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return [{"page": 0, "heading": "", "text": text, "source": "text", "locator": ""}]
    return []


def resolve_reference_snippets(
    course_dir: str | Path,
    refs: Sequence[str],
    *,
    budget_chars: int = 12000,
    per_material_chars: int = 3600,
) -> list[dict]:
    """把 ``source_refs`` 解析成可直接进提示词的片段（按页 / 幻灯片范围过滤）。

    与关键词检索的区别：这里的片段是用户**显式指定**的，属于硬依据，生成讲义时优先使用。
    """
    directory = Path(course_dir)
    entries = material_manifest_index(directory)
    snippets: list[dict] = []
    used = 0
    for raw in refs or []:
        parsed = parse_source_ref(str(raw))
        if parsed is None:
            continue
        entry = entries.get(str(parsed["material_id"]))
        if entry is None:
            continue
        budget = min(per_material_chars, max(0, budget_chars - used))
        if budget <= 0:
            break
        spent = 0
        for chunk in material_chunks(directory, entry):
            if not ref_matches_chunk(parsed, chunk):
                continue
            text = " ".join(str(chunk.get("text") or "").split())
            if not text:
                continue
            room = min(len(text), max(0, budget - spent))
            if room <= 0:
                break
            piece = text[:room]
            spent += len(piece)
            snippets.append(
                {
                    "material_id": str(parsed["material_id"]),
                    "filename": str(entry.get("filename") or ""),
                    "page": chunk.get("page"),
                    "heading": str(chunk.get("heading") or ""),
                    "locator": str(chunk.get("locator") or ""),
                    "text": piece,
                }
            )
        used += spent
    return snippets


def reference_label(snippet: dict) -> str:
    """一条参考资料在提示词里的抬头，例如 ``[m-1a2b] 课件A.pptx #s3``。"""
    identifier = str(snippet.get("material_id") or "")
    filename = str(snippet.get("filename") or "")
    locator = str(snippet.get("locator") or "")
    page = snippet.get("page")
    if not locator and page:
        locator = f"#p{page}"
    return f"[{identifier}] {filename}{locator}".strip()


# --- 逐页要点（全貌用摘要：覆盖整份课件，又不让长课本撑爆上下文） -----------
#: 缓存版本：摘要口径变了就 +1，旧缓存自动失效
DIGEST_VERSION = 1
#: 每页要点保留多少字（PPT 一页的正文本来就不长，摘要能覆盖整份）
DIGEST_GIST_CHARS = 110


def _gist_of(text: str, *, limit: int) -> str:
    """取一段话的开头作为要点（尽量在句号处收尾，不硬切）。"""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    for mark in ("。", "；", "！", "？", ".", ";", "!", "?"):
        position = cut.rfind(mark)
        if position >= limit * 0.5:
            return cut[: position + 1]
    return cut.rstrip() + "…"


def build_page_digest(chunks: Sequence[dict], *, gist_chars: int = DIGEST_GIST_CHARS) -> list[dict]:
    """把分块按页 / 幻灯片聚成「逐页要点」。

    纯本地抽取、不花 token、离线可用：幻灯片标题优先，其次取该页正文的开头。
    它是「全书全貌」，与 ``resolve_reference_snippets`` 的「原文摘录」互补。
    """
    grouped: dict[tuple[int, str], dict] = {}
    order: list[tuple[int, str]] = []
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        try:
            page = int(chunk.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        locator = str(chunk.get("locator") or "")
        key = (page, locator)
        if key not in grouped:
            grouped[key] = {"page": page, "locator": locator, "heading": "", "texts": []}
            order.append(key)
        entry = grouped[key]
        heading = " ".join(str(chunk.get("heading") or "").split())
        if heading and not entry["heading"]:
            entry["heading"] = heading
        text = " ".join(str(chunk.get("text") or "").split())
        if text:
            entry["texts"].append(text)
    items: list[dict] = []
    for key in order:
        entry = grouped[key]
        gist = _gist_of(" ".join(entry["texts"]), limit=gist_chars)
        if not gist and not entry["heading"]:
            continue
        items.append(
            {"page": entry["page"], "locator": entry["locator"], "heading": entry["heading"], "gist": gist}
        )
    return items


def material_page_digest(course_dir: str | Path, entry: dict) -> list[dict]:
    """某份资料的逐页要点；带 checksum 的磁盘缓存，资料没变就不重算。"""
    parsed_dir = Path(course_dir) / "materials" / "parsed"
    stem = Path(str(entry.get("filename") or "")).stem
    if not stem:
        return []
    checksum = str(entry.get("checksum") or "")
    cache_path = parsed_dir / f"{stem}.digest.json"
    if cache_path.exists():
        payload = file_repository.read_json_or_default(cache_path, {}) or {}
        if (
            int(payload.get("version") or 0) == DIGEST_VERSION
            and str(payload.get("checksum") or "") == checksum
            and isinstance(payload.get("items"), list)
        ):
            return [item for item in payload["items"] if isinstance(item, dict)]
    items = build_page_digest(material_chunks(course_dir, entry))
    try:
        parsed_dir.mkdir(parents=True, exist_ok=True)
        file_repository.write_json(
            cache_path, {"version": DIGEST_VERSION, "checksum": checksum, "items": items}
        )
    except OSError:  # 缓存写不进去不影响生成
        pass
    return items


def _sample_digest_items(items: Sequence[dict], budget: int) -> list[dict]:
    """等距抽样：宁可每几页给一条，也不要把后 400 页整段丢掉。"""
    if budget <= 0 or not items:
        return []
    heads: list[int] = []
    rest: list[int] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        identifier = str(item.get("material_id") or "")
        if identifier not in seen:
            seen.add(identifier)
            heads.append(index)  # 每份资料的第一条一定留：先给一个锚点
        else:
            rest.append(index)
    chosen: list[int] = []
    used = 0
    for index in heads:
        cost = int(items[index].get("cost") or 0)
        if used + cost <= budget:
            used += cost
            chosen.append(index)
    remaining = budget - used
    if rest and remaining > 0:
        average = sum(int(items[index].get("cost") or 0) for index in rest) / float(len(rest))
        capacity = max(1, int(remaining // max(average, 1.0)))
        if capacity >= len(rest):
            picked = set(rest)
        else:
            stride = len(rest) / float(capacity)
            picked = {rest[min(len(rest) - 1, int(position * stride))] for position in range(capacity)}
        for index in sorted(picked):
            cost = int(items[index].get("cost") or 0)
            if used + cost <= budget:
                used += cost
                chosen.append(index)
    return [items[index] for index in sorted(set(chosen))]


def resolve_reference_digest(
    course_dir: str | Path,
    refs: Sequence[str],
    *,
    budget_chars: int = 6000,
    gist_chars: int = DIGEST_GIST_CHARS,
    max_gist_chars: int = 90,
) -> dict:
    """把 ``source_refs`` 解析成「逐页要点」，**覆盖整个指定范围**。

    超过预算时等距抽样而不是截断——这正是「长课本前面看得到、后面全丢」的修法。
    """
    directory = Path(course_dir)
    entries = material_manifest_index(directory)
    collected: list[dict] = []
    for raw in refs or []:
        parsed = parse_source_ref(str(raw))
        if parsed is None:
            continue
        entry = entries.get(str(parsed["material_id"]))
        if entry is None:
            continue
        for item in material_page_digest(directory, entry):
            if not ref_matches_chunk(parsed, item):
                continue
            heading = str(item.get("heading") or "")
            gist = str(item.get("gist") or "")[:max_gist_chars]
            collected.append(
                {
                    "material_id": str(parsed["material_id"]),
                    "filename": str(entry.get("filename") or ""),
                    "page": item.get("page"),
                    "locator": str(item.get("locator") or ""),
                    "heading": heading,
                    "gist": gist,
                    "cost": len(heading) + len(gist) + 32,
                }
            )
    total = len(collected)
    if not collected:
        return {"items": [], "total_items": 0, "shown_items": 0, "truncated": False}
    budget = max(0, int(budget_chars))
    if sum(int(item["cost"]) for item in collected) <= budget:
        chosen = collected
    else:
        chosen = _sample_digest_items(collected, budget)
    return {
        "items": chosen,
        "total_items": total,
        "shown_items": len(chosen),
        "truncated": len(chosen) < total,
    }


def describe_reference_refs(course_dir: str | Path, refs: Sequence[str], *, limit: int = 3) -> str:
    """把 ``source_refs`` 说成人话：「课件A.pptx（幻灯片 3–4）、教材B.pdf」。

    界面要能一眼说清「这一节到底依据什么」，而不是甩一串 ``m-1a2b#p3-8``。
    """
    entries = material_manifest_index(course_dir)
    parts: list[str] = []
    for raw in refs or []:
        parsed = parse_source_ref(str(raw))
        if parsed is None:
            continue
        identifier = str(parsed["material_id"])
        name = str((entries.get(identifier) or {}).get("filename") or identifier)
        if parsed["start"] is None:
            parts.append(name)
            continue
        first, last = int(parsed["start"]), int(parsed["end"])
        if str(parsed.get("kind")) == "s":
            span = f"幻灯片 {first}" if first == last else f"幻灯片 {first}–{last}"
        else:
            span = f"第 {first} 页" if first == last else f"第 {first}–{last} 页"
        parts.append(f"{name}（{span}）")
    if not parts:
        return ""
    if len(parts) > limit:
        return "、".join(parts[:limit]) + f" 等 {len(parts)} 份"
    return "、".join(parts)
