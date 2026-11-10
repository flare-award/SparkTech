"""Проверка совместимости с минимальной поддерживаемой версией Python.

Приложение заявляет Python 3.10+, а CI прогоняет и 3.10, и 3.12. Тесты
помогают поймать использование синтаксиса и модулей новее 3.10 до того,
как это упадёт в CI.

Проверяется два класса проблем:

* синтаксис — через ``ast.parse(feature_version=(3, 10))``;
* API — по списку модулей/атрибутов, появившихся после 3.10
  (``tomllib``, ``asyncio.TaskGroup``, ``datetime.UTC`` и т. п.).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("sparktech", "tests", "scripts")

# Появилось в Python 3.11 / 3.12 и не должно встречаться в рабочем коде.
FORBIDDEN_APIS = {
    r"^\s*import\s+tomllib\b": "tomllib появился в 3.11 (в тестах — с фолбэком на tomli)",
    r"^\s*from\s+tomllib\s+import": "tomllib появился в 3.11 (в тестах — с фолбэком на tomli)",
    r"datetime\.UTC\b": "datetime.UTC появился в 3.11 (используйте timezone.utc)",
    r"^\s*from\s+datetime\s+import[^\n]*\bUTC\b": "datetime.UTC появился в 3.11 (используйте timezone.utc)",
    r"^\s*from\s+typing\s+import[^\n]*\bSelf\b": "typing.Self появился в 3.11",
    r"asyncio\.TaskGroup\b": "asyncio.TaskGroup появился в 3.11",
    r"\bExceptionGroup\b": "ExceptionGroup появился в 3.11",
    r"\bexcept\*": "синтаксис except* появился в 3.11",
    r"typing\.Self\b": "typing.Self появился в 3.11",
    r"from\s+enum\s+import\s+StrEnum": "StrEnum появился в 3.11",
    r"\bhashlib\.file_digest\b": "hashlib.file_digest появился в 3.11",
    r"typing\.assert_never\b": "typing.assert_never появился в 3.11",
}

# Разрешённые исключения:
#  * test_packaging.py — документированный фолбэк tomllib → tomli;
#  * этот файл — сам список запрещённых API, иначе он находит себя.
ALLOWED_FILES = {"tests/test_packaging.py", "tests/test_python_compat.py"}


def python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SOURCE_DIRS:
        files.extend(sorted((ROOT / directory).rglob("*.py")))
    return files


def test_there_are_files_to_check():
    assert len(python_files()) > 10


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_syntax_is_valid_for_python_310(path: Path):
    source = path.read_text(encoding="utf-8")
    try:
        ast.parse(source, filename=str(path), feature_version=(3, 10))
    except SyntaxError as exc:  # pragma: no cover — срабатывает только при регрессе
        raise AssertionError(f"{path}: синтаксис новее Python 3.10: {exc}") from exc


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_no_apis_newer_than_python_310(path: Path):
    rel = path.relative_to(ROOT).as_posix()
    if rel in ALLOWED_FILES:
        pytest.skip("файл содержит документированный фолбэк tomllib → tomli")
    source = path.read_text(encoding="utf-8")
    problems = []
    for pattern, reason in FORBIDDEN_APIS.items():
        for match in re.finditer(pattern, source, flags=re.MULTILINE):
            line = source[: match.start()].count("\n") + 1
            problems.append(f"строка {line}: {reason}")
    assert not problems, f"{rel}: " + "; ".join(problems)


def test_declared_minimum_python_is_310():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in pyproject
    # в CI должны проверяться обе границы заявленной поддержки
    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert '"3.10"' in workflow and '"3.12"' in workflow


def test_dev_requirements_cover_python_310():
    """Для 3.10 нужен tomli, иначе тест разбора pyproject будет пропущен."""
    reqs = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "tomli" in reqs
    assert 'python_version < "3.11"' in reqs


# Необязательный компонент нативного окна: импортируется в try/except,
# и без него приложение работает (интерфейс в браузере).
OPTIONAL_THIRD_PARTY = {"webview", "pytest", "pyinstaller"}
ALLOWED_THIRD_PARTY = {"sparktech/__main__.py": {"webview"}}


def test_runtime_has_no_third_party_imports():
    """Ядро и сервер обязаны работать на stdlib: иначе тесты на 3.10/3.12
    будут зависеть от состава окружения."""
    stdlib = set(getattr(__import__("sys"), "stdlib_module_names", ()))
    local = {"sparktech", "tests", "scripts"}
    offenders = []
    for path in python_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in {"tests/conftest.py"} or rel.startswith("tests/"):
            continue  # тестам разрешён pytest
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            allowed_here = ALLOWED_THIRD_PARTY.get(rel, set())
            for name in names:
                if name in stdlib or name in local or name in allowed_here:
                    continue
                if name in OPTIONAL_THIRD_PARTY and name in allowed_here:
                    continue
                offenders.append(f"{rel}: {name}")
    assert not offenders, f"сторонние импорты в рабочем коде: {offenders}"
