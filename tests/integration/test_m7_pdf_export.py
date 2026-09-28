"""M7 集成测试：真实 QtWebEngine 打印 PDF（M6 交付物验证）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bootstrap import bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services import export_service

pytestmark = [pytest.mark.integration, pytest.mark.ui]


def test_export_pdf_prints_real_pdf(qtbot, tmp_path: Path) -> None:
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from app.ui.export_helper import print_pdf

    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    course = tmp_path / "Courses" / "ATM 使用指南"

    result = export_service.export_pdf(course, printer=print_pdf, scope="all")

    assert result.path.exists()
    payload = result.path.read_bytes()
    assert payload.startswith(b"%PDF"), payload[:8]
    assert len(payload) > 2000
