"""Правила безопасной работы с путями внутри Android-устройства.

Все пути, попадающие в команды оболочки, обязаны сначала пройти через
:func:`quote_remote_path` (одинарные кавычки POSIX-оболочки) и проверку
:func:`is_safe_deletable`. Это защищает от инъекций через имена файлов
вида ``a; rm -rf /sdcard`` и не даёт удалить системные каталоги.
"""

from __future__ import annotations

POSIX_SAFE = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._-+/@,:=%"
)

# Корни, которые трогать нельзя ни при каких обстоятельствах.
PROTECTED_EXACT = {
    "/",
    "/system",
    "/vendor",
    "/product",
    "/odm",
    "/oem",
    "/apex",
    "/data",
    "/data/app",
    "/data/data",
    "/data/system",
    "/data/local",
    "/data/misc",
    "/sdcard",
    "/storage",
    "/storage/emulated",
    "/mnt",
    "/mnt/media_rw",
    "/proc",
    "/sys",
    "/dev",
    "/etc",
    "/bin",
    "/sbin",
    "/root",
    "/config",
    "/metadata",
}

# Каталоги, содержимое которых нельзя удалять: система, данные приложений.
PROTECTED_PREFIXES = (
    "/system",
    "/vendor",
    "/product",
    "/odm",
    "/oem",
    "/apex",
    "/data/app",
    "/data/data",
    "/data/system",
    "/data/local",
    "/data/misc",
    "/data/adb",
    "/data/tombstones",
    "/proc",
    "/sys",
    "/dev",
    "/etc",
    "/bin",
    "/sbin",
    "/config",
    "/metadata",
    "/mnt/media_rw",
    "/storage/emulated/0/android/data/",
    "/storage/emulated/0/android/obb/",
    "/sdcard/android/data/",
    "/sdcard/android/obb/",
)

# Области, которые SparkTech вообще не сканирует (в них нет осмысленного
# пользовательского мусора, а ошибок доступа слишком много).
SKIP_SCAN_PREFIXES = (
    "/proc",
    "/sys",
    "/dev",
    "/acct",
    "/d",
)


class UnsafePathError(ValueError):
    """Путь отклонён защитой SparkTech."""


def quote_remote_path(path: str) -> str:
    """Экранировать путь для POSIX-оболочки внутри ``adb shell``.

    Используется идиома ``'…'\\''…'``: закрыть кавычку, экранировать
    одинарную кавычку, снова открыть. Работает для пробелов, ``$``, ``;``,
    ``*``, переводов строк и кириллицы.
    """
    if not isinstance(path, str) or not path:
        raise UnsafePathError("Пустой путь")
    if "\x00" in path or "\r" in path:
        raise UnsafePathError("Путь содержит запрещённые управляющие символы")
    return "'" + path.replace("'", "'\\''") + "'"


def normalize_remote(path: str) -> str:
    """Привести путь к виду ``/a/b`` без ``//`` и ``/./`` и без хвостового ``/``."""
    if not path:
        return ""
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    parts: list[str] = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def _lower(path: str) -> str:
    # Android-ФС регистрозависимы, но /sdcard и /storage/emulated/0 —
    # один и тот же том; сравнения защиты делаем без учёта регистра.
    return normalize_remote(path).lower()


def is_protected(path: str) -> bool:
    """Является ли путь системным/защищённым (удалять нельзя)."""
    low = _lower(path)
    if low in PROTECTED_EXACT:
        return True
    for prefix in PROTECTED_PREFIXES:
        if low == prefix.rstrip("/") or low.startswith(prefix):
            return True
    return False


def is_under_allowed_root(path: str, roots: list[str]) -> bool:
    low = _lower(path)
    for root in roots:
        r = _lower(root)
        if low == r or low.startswith(r.rstrip("/") + "/"):
            return True
    return False


def is_safe_deletable(path: str) -> tuple[bool, str]:
    """Можно ли пытаться удалить файл по этому пути.

    Возвращает (разрешено, причина_отказа). Проверяется:
      * путь абсолютный и нормализованный;
      * не защищённый каталог;
      * не корень тома;
      * нет управляющих символов.
    """
    if not path or not path.startswith("/"):
        return False, "Путь не является абсолютным"
    if "\x00" in path or "\n" in path or "\r" in path:
        return False, "Путь содержит управляющие символы"
    if normalize_remote(path) != path.rstrip("/") or path.endswith("/"):
        # Мы всегда храним уже нормализованные пути файлов.
        return False, "Путь не нормализован"
    if is_protected(path):
        return False, "Системный или защищённый каталог — удаление запрещено SparkTech"
    return True, ""


def strip_sdcard_prefix(path: str) -> str:
    """``/sdcard/Download/a`` -> ``/storage/emulated/0/Download/a``."""
    low = path.lower()
    for prefix in ("/sdcard", "/storage/self/primary"):
        if low == prefix:
            return "/storage/emulated/0"
        if low.startswith(prefix + "/"):
            return "/storage/emulated/0" + path[len(prefix):]
    return path
