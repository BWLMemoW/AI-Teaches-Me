"""环境基线自检（M0 开工前的冒烟测试）。

对应《技术文档》§1.6 运行环境基线 与《项目文档》§6.3 依赖清单。
本文件不访问网络，用于确认本机环境满足开工条件。
"""

from __future__ import annotations

import importlib
import sqlite3
import sys

import pytest

pytestmark = pytest.mark.no_network

REQUIRED_DISTRIBUTIONS = [
    "PySide6",      # 桌面框架（含 QtWebEngine / QtSvg）
    "openai",       # OpenAI 兼容客户端
    "httpx",        # 超时 / 代理 / 流式细节控制
    "pydantic",     # 结构化输出校验
    "keyring",      # 密钥安全存储（Windows DPAPI / 凭据库）
    "platformdirs",  # 跨平台目录规范
    "pypdf",        # PDF 解析
    "pptx",         # PPTX 解析（python-pptx）
    "docx",         # DOCX 解析（python-docx）
    "PIL",          # 图片处理（Pillow）
    "markdown",     # 服务端 Markdown → HTML
    "pygments",     # 代码高亮
]

MIN_PYTHON = (3, 11)
MIN_SQLITE = (3, 34)  # FTS5 trigram 需要 SQLite >= 3.34（见《技术文档》§3.7）


def test_python_version_baseline() -> None:
    assert sys.version_info >= MIN_PYTHON, f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+"


def test_sqlite_version_supports_trigram() -> None:
    current = tuple(int(part) for part in sqlite3.sqlite_version.split(".")[:2])
    assert current >= MIN_SQLITE, f"FTS5 trigram 需要 SQLite {MIN_SQLITE[0]}.{MIN_SQLITE[1]}+"


def test_fts5_trigram_can_match_chinese() -> None:
    """中文检索必须走 trigram，这里验证分词器真的可用（不是只建表成功）。"""
    with sqlite3.connect(":memory:") as con:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='trigram')")
        con.execute("INSERT INTO t VALUES (?)", ("矩阵乘法不是逐项相乘",))
        assert con.execute("SELECT count(*) FROM t WHERE t MATCH ?", ("逐项相乘",)).fetchone()[0] == 1


@pytest.mark.parametrize("module_name", REQUIRED_DISTRIBUTIONS)
def test_required_dependency_importable(module_name: str) -> None:
    assert importlib.util.find_spec(module_name) is not None, f"缺少依赖：{module_name}"


def test_qt_core_importable_with_expected_major_version() -> None:
    from PySide6 import QtCore

    assert int(QtCore.qVersion().split(".")[0]) >= 6


def test_keyring_has_system_backend() -> None:
    import keyring

    assert keyring.get_keyring().__module__ != "keyring.backends.fail", "未找到可用的系统凭据库后端（FR-02 密钥存储依赖它）"
