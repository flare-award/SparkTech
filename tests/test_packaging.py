"""Проверки упаковки: spec-файл, скрипты сборки, статика в пакете.

Собрать .exe в этой среде нельзя (Linux без libpython), поэтому конфигурация
проверяется статически: синтаксис spec-файла, наличие ресурсов и согласованность
путей между сборкой и кодом.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# tomllib появился только в Python 3.11. Матрица CI включает 3.10, поэтому
# используем его, если он есть, иначе — tomli, а при отсутствии обоих тест
# разбора pyproject пропускается (остальные проверки упаковки выполняются).
try:  # pragma: no cover — зависит от версии Python
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "SparkTech.spec"


def test_spec_is_valid_python_and_targets_entrypoint():
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    assert isinstance(tree, ast.Module)
    text = SPEC.read_text(encoding="utf-8")
    assert '"__main__.py"' in text
    assert 'name="SparkTech"' in text
    assert "console=False" in text  # GUI без консольного окна


def test_spec_includes_static_assets():
    text = SPEC.read_text(encoding="utf-8")
    assert '"sparktech/web/static"' in text
    # Путь в spec и в коде сервера должны совпадать
    server = (ROOT / "sparktech" / "web" / "server.py").read_text(encoding="utf-8")
    assert '_MEIPASS' in server and "sparktech" in server


def test_static_dir_resolves_from_sources():
    from sparktech.web.server import STATIC_DIR

    assert (STATIC_DIR / "index.html").is_file()
    assert (STATIC_DIR / "app.js").is_file()
    assert (STATIC_DIR / "styles.css").is_file()


def test_version_info_is_in_repository():
    """Файл метаданных версии не должен лежать в gitignored build/."""
    version_file = ROOT / "packaging" / "version_info.txt"
    assert version_file.is_file()
    text = version_file.read_text(encoding="utf-8")
    assert "VSVersionInfo" in text and "SparkTech" in text
    spec = SPEC.read_text(encoding="utf-8")
    assert 'packaging" / "version_info.txt"' in spec
    # build/ целиком игнорируется, поэтому файл там бы не попал в репозиторий
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "build/" in ignore


def test_build_scripts_setup_python_encoding():
    """Скрипты сборки содержат кириллицу: на Windows консоль не должна падать."""
    ps1 = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8-sig")
    bat = (ROOT / "scripts" / "build_windows.bat").read_text(encoding="utf-8")
    assert "Console]::OutputEncoding" in ps1
    assert "chcp 65001" in bat
    # .bat в UTF-8 без BOM, но без кириллицы в исполняемых строках
    assert "Сборка SparkTech" in bat


def test_build_scripts_exist_and_mention_license():
    ps1 = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    bat = (ROOT / "scripts" / "build_windows.bat").read_text(encoding="utf-8")
    for script in (ps1, bat):
        assert "PyInstaller" in script
        assert "developer.android.com" in script
        assert "adb" in script.lower()
    assert "License Agreement" in ps1


def test_build_scripts_do_not_download_third_party_binaries():
    """Программа не должна незаметно скачивать adb: только ручное размещение."""
    for name in ("scripts/build_windows.ps1", "scripts/build_windows.bat", ".github/workflows/build.yml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"(Invoke-WebRequest|curl\s+.*platform-tools|wget\s+.*platform-tools)", text), name


def test_ci_builds_windows_exe():
    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert "windows-latest" in workflow
    assert "PyInstaller" in workflow
    assert "dist/SparkTech.exe" in workflow
    assert "pytest" in workflow


def test_pyproject_packages_static():
    if tomllib is None:  # pragma: no cover — Python 3.10 без tomli
        pytest.skip("нет tomllib/tomli для разбора pyproject.toml")
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "sparktech"
    assert data["project"]["requires-python"].startswith(">=3.10")
    assert data["project"]["dependencies"] == []  # ядро на stdlib
    assert "static/*" in data["tool"]["setuptools"]["package-data"]["sparktech.web"]
    assert data["project"]["scripts"]["sparktech"].endswith("main")


def test_entrypoint_importable():
    import sparktech.__main__ as entry

    assert callable(entry.main)


def test_default_bind_is_loopback():
    import inspect

    from sparktech.web.server import create_server

    signature = inspect.signature(create_server)
    assert signature.parameters["host"].default == "127.0.0.1"


def test_readme_covers_required_topics():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for topic in ("Установк", "Сборк", ".exe", "adb", "Platform-Tools", "Ограничен", "безопасн"):
        assert topic.lower() in readme.lower(), f"в README нет раздела про «{topic}»"
