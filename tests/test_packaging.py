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
    # точка входа — обёртка с абсолютным импортом (см. test_entrypoint_wrapper)
    assert '"run_sparktech.py"' in text
    assert "packages\" / \"__main__.py\"" not in text
    assert 'name="SparkTech"' in text
    assert "console=False" in text  # GUI без консольного окна


def test_spec_output_is_encoding_safe():
    """Сборка на русской Windows падала с UnicodeEncodeError при печати
    кириллицы в консоль cp1252/cp866 — весь вывод spec должен быть безопасным."""
    text = SPEC.read_text(encoding="utf-8")
    assert "def note(message" in text
    assert 'errors="replace"' in text
    # ни одного «голого» print с русским текстом
    import re

    bare = re.findall(
        r"^\s*print\(\s*[\"'][^\"']*[\u0410-\u044f]",
        text,
        flags=re.MULTILINE,
    )
    assert not bare, f"print с кириллицей вне note(): {bare}"
    assert "note(" in text


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
    assert "$env:PYTHONUTF8" in ps1
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
    """Программа и сборка не должны незаметно скачивать adb: только вручную.

    Разрешён локальный HTTP-опрос собственного сервера (smoke-тест exe),
    запрещено скачивание сторонних бинарников.
    """
    patterns = (
        r"Invoke-WebRequest[^\n]*platform-tools",
        r"curl[^\n]*platform-tools",
        r"wget[^\n]*platform-tools",
        r"Start-BitsTransfer",
        r"DownloadFile",
    )
    for name in ("scripts/build_windows.ps1", "scripts/build_windows.bat", ".github/workflows/build.yml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for pattern in patterns:
            assert not re.search(pattern, text, flags=re.IGNORECASE), f"{name}: {pattern}"


def test_build_scripts_mention_official_page_for_manual_download():
    """Пользователю показывают официальную страницу, а не ссылку на зеркало."""
    ps1 = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    assert "https://developer.android.com/tools/releases/platform-tools" in ps1


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


def test_entrypoint_wrapper_uses_absolute_import():
    """PyInstaller не может запустить пакетный __main__.py с относительными
    импортами — это проверено в CI и зафиксировано этим тестом."""
    wrapper = (ROOT / "run_sparktech.py").read_text(encoding="utf-8")
    assert "from sparktech.__main__ import main" in wrapper
    main_module = (ROOT / "sparktech" / "__main__.py").read_text(encoding="utf-8")
    assert "from . import APP_NAME" in main_module  # относительный импорт остаётся
    # обёртка должна быть в состоянии сообщить версию
    import subprocess
    import sys as _sys

    out = subprocess.run(
        [_sys.executable, str(ROOT / "run_sparktech.py"), "--version"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "1.0.0" in out.stdout


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


def spec_excludes() -> set[str]:
    """Прочитать excludes= из SparkTech.spec (без запуска PyInstaller)."""
    text = SPEC.read_text(encoding="utf-8")
    block = re.search(r"excludes=\[([^\]]*)\]", text)
    assert block, "в spec нет excludes="
    return set(re.findall(r"[\"']([^\"']+)[\"']", block.group(1)))


def app_import_closure() -> set[str]:
    """Все stdlib-модули, которые реально импортирует приложение.

    Импортируем точку входа и сервер во временном процессе и смотрим, что
    оказалось в sys.modules: так ловится именно транзитивная зависимость
    (например, http.server → email), а не только прямые импорты.
    """
    import subprocess
    import sys as _sys

    code = (
        "import json, sys;"
        "import sparktech.__main__;"
        "import sparktech.web.server;"
        "from sparktech.core import adb, classify, deleter, jobs, models, optimizer, process, remote_path, scanner, store;"
        "top = {m.split('.')[0] for m in sys.modules};"
        "print(json.dumps(sorted(top)))"
    )
    out = subprocess.run(
        [_sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert out.returncode == 0, out.stderr
    import json as _json

    return set(_json.loads(out.stdout))


def test_spec_never_excludes_modules_app_needs():
    """Собранный .exe упал с ModuleNotFoundError: No module named 'email',
    потому что spec исключал email, а http.server его импортирует."""
    excluded = spec_excludes()
    needed = app_import_closure()
    # stdlib-модули, без которых приложение не запускается
    must_keep = {"email", "http", "json", "socket", "threading", "urllib", "secrets"}
    assert must_keep <= needed, f"неожиданно не импортируется: {must_keep - needed}"
    broken = sorted((excluded & needed) | (excluded & must_keep))
    assert not broken, (
        f"spec исключает модули, нужные приложению при старте: {broken}. "
        "Собранный .exe упадёт с ModuleNotFoundError."
    )


def test_spec_excludes_only_safe_modules():
    allowed = {"tkinter", "unittest", "pytest", "pydoc", "test", "idlelib", "turtle"}
    extra = sorted(spec_excludes() - allowed)
    assert not extra, (
        f"подозрительные исключения в spec: {extra}. Исключать можно только модули, "
        "не нужные приложению и его зависимостям."
    )


def test_frozen_failure_is_diagnosable():
    """У оконного .exe нет консоли: ошибка запуска должна попадать в файл.

    Иначе пользователь (и CI) видит только «ничего не произошло».
    """
    main_module = (ROOT / "sparktech" / "__main__.py").read_text(encoding="utf-8")
    assert "_write_diagnostic" in main_module
    assert "crash.log" in main_module       # фатальная ошибка
    assert "startup.log" in main_module     # успешный старт + порт
    assert "traceback.format_exception" in main_module

    # реальная проверка: фатальная ошибка пишется в файл и не роняет процесс
    import subprocess
    import sys as _sys
    import tempfile
    import os as _os

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(_os.environ, XDG_STATE_HOME=tmp, LOCALAPPDATA=tmp)
        code = (
            "import sparktech.__main__ as m\n"
            "def boom(*a, **k):\n"
            "    raise RuntimeError('тестовая ошибка запуска')\n"
            "m.AppContext = boom\n"
            "raise SystemExit(m.main(['--no-window']))\n"
        )
        proc = subprocess.run(
            [_sys.executable, "-c", code], capture_output=True, text=True,
            cwd=str(ROOT), env=env, timeout=120,
        )
        assert proc.returncode == 1, proc.stderr
        crash = (Path(tmp) / "sparktech" / "crash.log")
        assert crash.is_file(), f"нет crash.log, stderr={proc.stderr[:400]}"
        text = crash.read_text(encoding="utf-8")
        assert "RuntimeError" in text and "тестовая ошибка запуска" in text
