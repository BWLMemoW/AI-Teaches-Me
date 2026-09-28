"""把前端渲染依赖下载进 app/web/vendor/（《技术文档》§1.5 红线 4：禁止 CDN，必须本地）。

用法：

    python scripts/fetch_vendor.py                 # 下载 + 校验 + 入库（已存在的包会跳过）
    python scripts/fetch_vendor.py --only katex    # 只处理某个包
    python scripts/fetch_vendor.py --force         # 忽略已入库文件，重新下载
    python scripts/fetch_vendor.py --check         # 只检查关键文件是否齐全

安全约定（下载前请阅读）：

1. 只从 **npm 官方 registry**（https://registry.npmjs.org）取包；
2. 校验 registry 声明的 **sha512 integrity**，校验不通过直接失败，不落盘；
3. 只解包需要的那几个文件 / 目录，**不执行**下载内容里的任何代码；
4. 分块读取 + 失败重试，避免大包（KaTeX 含字体）中途超时；
5. 版本与来源写入 ``app/web/vendor/VENDOR.json``，便于复现与升级。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REGISTRY = "https://registry.npmjs.org"
REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = REPO_ROOT / "app" / "web" / "vendor"
CHUNK_SIZE = 64 * 1024
PROGRESS_STEP = 512 * 1024

PACKAGES: list[dict[str, object]] = [
    {
        "name": "markdown-it",
        "version": "14.1.0",
        "target": "markdown-it",
        "purpose": "前端 Markdown → HTML（GFM 表格 / 任务列表 / 脚注 / 删除线）",
        "files": {
            "package/dist/markdown-it.min.js": "markdown-it.min.js",
            "package/LICENSE": "LICENSE",
        },
        "trees": {},
    },
    {
        "name": "katex",
        "version": "0.16.11",
        "target": "katex",
        "purpose": "LaTeX 公式渲染（含本地字体文件，离线可用）",
        "files": {
            "package/dist/katex.min.js": "katex.min.js",
            "package/dist/katex.min.css": "katex.min.css",
            "package/LICENSE": "LICENSE",
        },
        "trees": {"package/dist/fonts": "fonts"},
    },
    {
        "name": "@highlightjs/cdn-assets",
        "version": "11.10.0",
        "target": "highlight.js",
        "purpose": "代码高亮（浏览器可直接引用的压缩发行版）",
        "files": {
            "package/highlight.min.js": "highlight.min.js",
            "package/styles/atom-one-light.min.css": "atom-one-light.min.css",
            "package/styles/atom-one-dark.min.css": "atom-one-dark.min.css",
            "package/LICENSE": "LICENSE",
        },
        "trees": {},
    },
]


def log(message: str) -> None:
    """打印日志（兼容 GBK 控制台：编码不了就替换字符，不让脚本因日志中断）。"""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(message.encode(encoding, "replace").decode(encoding, "replace"), flush=True)


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "atm-vendor-fetch/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - 固定官方 registry
        return json.loads(response.read().decode("utf-8"))


def fetch_bytes(url: str, *, attempts: int = 5, timeout: float = 30.0) -> bytes:
    """分块下载；单次卡住超过 timeout 即重试，避免大包整体超时。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "atm-vendor-fetch/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                total = int(response.headers.get("Content-Length") or 0)
                chunks: list[bytes] = []
                received = 0
                next_mark = PROGRESS_STEP
                while True:
                    block = response.read(CHUNK_SIZE)
                    if not block:
                        break
                    chunks.append(block)
                    received += len(block)
                    if received >= next_mark:
                        total_text = f"{total // 1024} KB" if total else "未知大小"
                        log(f"    ... {received // 1024} KB / {total_text}")
                        next_mark += PROGRESS_STEP
                return b"".join(chunks)
        except Exception as exc:  # noqa: BLE001 - 网络抖动都要重试
            last_error = exc
            log(f"    下载中断（第 {attempt}/{attempts} 次）：{type(exc).__name__}: {exc}")
            time.sleep(min(2.0 * attempt, 8.0))
    raise SystemExit(f"下载失败：{last_error}")


def verify_integrity(payload: bytes, integrity: str) -> None:
    if not integrity:
        raise SystemExit("registry 未提供 integrity，拒绝入库（安全优先）")
    algorithm, _, digest = integrity.partition("-")
    if algorithm != "sha512":
        raise SystemExit(f"不支持的 integrity 算法：{algorithm}")
    actual = base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")
    if actual != digest:
        raise SystemExit("sha512 校验不通过，已中止（不写入任何文件）")


def resolve_version(meta: dict, wanted: str) -> tuple[str, str]:
    versions = meta.get("versions", {})
    if wanted in versions:
        return wanted, versions[wanted]["dist"]["tarball"]
    latest = meta.get("dist-tags", {}).get("latest")
    if not latest or latest not in versions:
        raise SystemExit(f"registry 上找不到 {wanted}，也没有可用的 latest")
    log(f"  ! 指定版本 {wanted} 不存在，回退到 latest {latest}")
    return latest, versions[latest]["dist"]["tarball"]


def extract(payload: bytes, spec: dict[str, object], target_dir: Path) -> list[str]:
    written: list[str] = []
    files = dict(spec["files"])  # type: ignore[arg-type]
    trees = dict(spec["trees"])  # type: ignore[arg-type]
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = set(archive.getnames())
        for member_name, output_name in files.items():
            if member_name not in names:
                raise SystemExit(f"包内缺少文件：{member_name}")
            handle = archive.extractfile(member_name)
            if handle is None:
                raise SystemExit(f"无法读取：{member_name}")
            destination = target_dir / str(output_name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(handle.read())
            written.append(str(destination.relative_to(VENDOR_DIR)))
        for member_name, output_name in trees.items():
            base = target_dir / str(output_name)
            base.mkdir(parents=True, exist_ok=True)
            prefix = str(member_name).rstrip("/") + "/"
            for member in archive.getmembers():
                if not member.isfile() or not member.name.startswith(prefix):
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                destination = base / member.name[len(prefix):]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(handle.read())
                written.append(str(destination.relative_to(VENDOR_DIR)))
    return written


def already_installed(spec: dict[str, object]) -> bool:
    target = VENDOR_DIR / str(spec["target"])
    for output_name in dict(spec["files"]).values():  # type: ignore[arg-type]
        if not (target / str(output_name)).exists():
            return False
    for output_name in dict(spec["trees"]).values():  # type: ignore[arg-type]
        directory = target / str(output_name)
        if not directory.is_dir() or not any(directory.iterdir()):
            return False
    return True


def load_existing_manifest() -> dict[str, dict[str, str]]:
    """读取已入库清单，便于在只处理单个包时保留其它包的记录。"""
    path = VENDOR_DIR / "VENDOR.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = {}
    for item in data.get("packages", []):
        if isinstance(item, dict) and item.get("name"):
            entries[str(item["name"])] = {str(key): str(value) for key, value in item.items()}
    return entries


def record_provenance(name: str, wanted: str) -> dict[str, str] | None:
    """已入库的包：只读 registry 元信息补齐版本与 sha512 来源（不重新下载）。"""
    try:
        meta = fetch_json(f"{REGISTRY}/{name.replace('/', '%2f')}")
        version, tarball = resolve_version(meta, wanted)
    except Exception as exc:  # noqa: BLE001
        log(f"  读取 registry 元信息失败，保留旧清单条目：{exc}")
        return None
    return {
        "name": name,
        "version": version,
        "source": tarball,
        "integrity": meta["versions"][version]["dist"].get("integrity", ""),
        "purpose": "",
        "files": "已入库",
    }


def fetch_all(only: str | None = None, force: bool = False) -> int:
    manifest: list[dict[str, str]] = []
    existing = load_existing_manifest()
    for spec in PACKAGES:
        name = str(spec["name"])
        if only and only not in (name, str(spec["target"])):
            continue
        target = VENDOR_DIR / str(spec["target"])
        log(f"[vendor] {name} -> app/web/vendor/{spec['target']}/")
        if not force and already_installed(spec):
            log("  已存在，跳过（用 --force 可重新下载）")
            preserved = existing.get(name)
            if preserved:
                manifest.append(preserved)
            else:
                provenance = record_provenance(name, str(spec["version"]))
                manifest.append(
                    provenance
                    or {"name": name, "version": "已入库", "source": "", "integrity": "", "purpose": str(spec["purpose"]), "files": "0"}
                )
            continue

        meta = fetch_json(f"{REGISTRY}/{name.replace('/', '%2f')}")
        version, tarball = resolve_version(meta, str(spec["version"]))
        integrity = meta["versions"][version]["dist"].get("integrity", "")
        payload = fetch_bytes(tarball)
        verify_integrity(payload, integrity)
        written = extract(payload, spec, target)
        manifest.append(
            {
                "name": name,
                "version": version,
                "source": tarball,
                "integrity": integrity,
                "purpose": str(spec["purpose"]),
                "files": str(len(written)),
            }
        )
        log(f"  [ok] {version}（{len(written)} 个文件，sha512 校验通过）")

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    (VENDOR_DIR / "VENDOR.json").write_text(
        json.dumps(
            {
                "note": "由 scripts/fetch_vendor.py 生成；运行时只允许本地引用，禁止 CDN。",
                "packages": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    log("[vendor] 完成，清单写入 app/web/vendor/VENDOR.json")
    return 0


def check() -> int:
    expected = {
        "markdown-it/markdown-it.min.js",
        "katex/katex.min.js",
        "katex/katex.min.css",
        "highlight.js/highlight.min.js",
        "highlight.js/atom-one-light.min.css",
        "highlight.js/atom-one-dark.min.css",
    }
    missing = sorted(item for item in expected if not (VENDOR_DIR / item).exists())
    font_dir = VENDOR_DIR / "katex" / "fonts"
    fonts = list(font_dir.glob("*.woff2")) if font_dir.is_dir() else []
    if missing:
        log("[vendor] 缺失：" + "、".join(missing))
        return 1
    log(f"[vendor] 关键文件齐全（KaTeX woff2 字体 {len(fonts)} 个）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载前端渲染依赖到 app/web/vendor/")
    parser.add_argument("--check", action="store_true", help="只检查已入库文件是否齐全")
    parser.add_argument("--only", default=None, help="只处理指定包（markdown-it / katex / highlight.js）")
    parser.add_argument("--force", action="store_true", help="忽略已入库文件，重新下载")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    return fetch_all(only=args.only, force=args.force)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        log(f"[vendor] 网络请求失败：{exc}")
        raise SystemExit(2) from exc
