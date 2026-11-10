"""Приложения на телефоне: реальные метаданные для отдельной очистки.

Что важно понимать про честность этого модуля:

* **Удалять приложения** SparkTech не имеет права и не будет: удаление
  пакета — действие пользователя, а не «оптимизация». Максимум — открыть
  экран приложения в настройках телефона.
* **Внутренние данные и кэш приложений** (``/data/data``) недоступны без
  root, поэтому «очистить кэш приложения» из программы нельзя — только
  показать инструкцию.
* **Что можно**: показать список установленных пакетов, реальные названия из
  ресурсов, иконки из APK, размер APK и объём данных, дату установки и
  обновления, время последнего запуска, системность и категорию.

Источники данных (оба официальные, без root):

* ``dumpsys package packages`` — детальная информация по каждому пакету
  (``versionName``, ``firstInstallTime``, ``lastUpdateTime``, ``lastTimeUsed``,
  флаги). Используется, если доступен: содержит всё в одном вызове.
* ``pm list packages -f -U -d`` + ``dumpsys diskstats`` — запасной вариант:
  путь к APK, UID, а размеры «приложение/данные/кэш» приходят из diskstats
  в виде base64-сжатого массива.

Реальные названия и иконки берутся из APK:

* название — через ``assets`` APK читается ``resources.arsc`` (полноценный
  парсер бинарных ресурсов Android, см. :mod:`sparktech.core.arsc`);
* иконка — PNG/WebP-файл из APK по идентификатору ресурса иконки.

Если что-то из этого прочитать не удалось, поле остаётся пустым, и интерфейс
показывает нейтральную заглушку — подделывать «красивые» иконки нельзя.
"""

from __future__ import annotations

import base64
import re
import zlib
from dataclasses import dataclass, field
from typing import Any

from .adb import AdbClient
from .arsc import ApkResources, ResourceTableError
from .process import CommandCancelled, CommandError, CommandTimeout
from .remote_path import quote_remote_path

# Категории, которые можно определить объективно: по системности пакета и
# известным префиксам. Всё остальное — «Приложение».
SYSTEM_PREFIXES = (
    "com.android.",
    "com.google.android.",
    "com.samsung.android.",
    "com.miui.",
    "com.xiaomi.",
    "com.huawei.",
    "com.oppo.",
    "com.vivo.",
    "com.coloros.",
    "com.oneplus.",
    "org.codeaurora.",
)

# Частые категории — по префиксу пакета. Это не «магия»: категория видна в
# интерфейсе и всегда сопровождается именем пакета.
CATEGORY_RULES: list[tuple[str, str, str]] = [
    ("com.whatsapp", "messenger", "Мессенджер"),
    ("org.telegram.messenger", "messenger", "Мессенджер"),
    ("com.viber", "messenger", "Мессенджер"),
    ("ru.ok.", "social", "Соцсеть"),
    ("com.vk.", "social", "Соцсеть"),
    ("com.instagram", "social", "Соцсеть"),
    ("com.facebook", "social", "Соцсеть"),
    ("com.twitter", "social", "Соцсеть"),
    ("com.zhiliaoapp.musically", "video", "Видео"),
    ("com.ss.android.ugc.trill", "video", "Видео"),
    ("com.google.android.youtube", "video", "Видео"),
    ("com.netflix", "video", "Видео"),
    ("com.google.android.apps.photos", "photo", "Фото"),
    ("com.spotify", "audio", "Музыка"),
    ("com.google.android.apps.maps", "travel", "Навигация"),
    ("ru.yandex.maps", "travel", "Навигация"),
    ("com.yandex.browser", "browser", "Браузер"),
    ("com.android.chrome", "browser", "Браузер"),
    ("org.mozilla.firefox", "browser", "Браузер"),
    ("com.google.android.gm", "mail", "Почта"),
    ("ru.yandex.mail", "mail", "Почта"),
    ("com.google.android.apps.docs", "office", "Документы"),
    ("com.microsoft.office", "office", "Документы"),
    ("com.android.vending", "system", "Магазин приложений"),
    ("com.google.android.gms", "system", "Системный компонент"),
    ("com.google.android.apps.wellbeing", "system", "Системный компонент"),
]

# Сколько APK читать с устройства за одно построение списка и сколько байт
# из каждого. Названия/иконки лежат в начале архива, поэтому чтения начала
# файла достаточно; для очень больших APK чтение ограничено.
MAX_RESOURCE_APPS = 120
APK_READ_BYTES = 3 * 1024 * 1024

# Пакеты, которые нельзя предлагать даже для «открыть настройки» без
# предупреждения: от них зависит работа телефона.
CRITICAL_PACKAGES = {
    "android",
    "com.android.systemui",
    "com.android.settings",
    "com.android.phone",
    "com.android.bluetooth",
    "com.android.providers.media",
    "com.android.providers.contacts",
    "com.android.providers.telephony",
    "com.android.providers.downloads",
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.android.vending",
    "com.android.keychain",
    "com.android.shell",
}


def category_for(package: str, *, is_system: bool) -> tuple[str, str]:
    """Категория приложения: (ключ, подпись)."""
    low = package.lower()
    for prefix, key, label in CATEGORY_RULES:
        if low.startswith(prefix):
            return key, label
    if is_system or low.startswith(SYSTEM_PREFIXES):
        return "system", "Системное"
    return "app", "Приложение"


@dataclass
class AppInfo:
    package: str
    label: str = ""                 # реальное название из ресурсов APK
    version_name: str = ""
    apk_path: str = ""
    apk_size: int = 0
    data_size: int = 0              # /data/data без кэша (по diskstats)
    cache_size: int = 0
    code_size: int = 0
    installed_at: int = 0           # epoch ms — первая установка
    updated_at: int = 0             # epoch ms — последнее обновление
    last_used_at: int = 0           # epoch ms — последний запуск (может быть 0)
    is_system: bool = False
    enabled: bool = True
    uid: str = ""
    category_key: str = "app"
    category_label: str = "Приложение"
    critical: bool = False
    icon_available: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        """Полный занимаемый объём (APK + данные + кэш), как его считает Android."""
        return self.apk_size + self.data_size + self.cache_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "label": self.label or self.package,
            "label_known": bool(self.label),
            "version_name": self.version_name,
            "apk_path": self.apk_path,
            "apk_size": self.apk_size,
            "data_size": self.data_size,
            "cache_size": self.cache_size,
            "code_size": self.code_size,
            "total_size": self.total_size,
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "is_system": self.is_system,
            "enabled": self.enabled,
            "uid": self.uid,
            "category_key": self.category_key,
            "category_label": self.category_label,
            "critical": self.critical,
            "icon_available": self.icon_available,
            "notes": self.notes,
        }


# --------------------------------------------------------------- парсинг dumpsys


def _ms(value: str) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return 0


def parse_dumpsys_packages(text: str) -> dict[str, dict[str, str]]:
    """Разобрать ``dumpsys package packages`` в словарь по имени пакета.

    Формат между версиями Android отличается, но нужные нам ключи стабильны:
    ``versionName``, ``firstInstallTime``, ``lastUpdateTime``, ``lastTimeUsed``,
    ``flags``, ``codePath``, ``pkgFlags``. Разбор намеренно «мягкий»: незнакомые
    строки игнорируются, а не ломают весь список.
    """
    packages: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    header = re.compile(r"^\s*Package \[([^\]]+)\]")
    for raw in text.splitlines():
        match = header.match(raw)
        if match:
            current = packages.setdefault(match.group(1), {})
            continue
        if current is None:
            continue
        line = raw.strip()
        if not line:
            if "codePath" in current:
                # Пустая строка после блока пакета — обычно конец раздела.
                continue
            continue
        for key, pattern in (
            ("versionName", r"versionName=(\S+)"),
            ("codePath", r"codePath=(\S+)"),
            ("firstInstallTime", r"firstInstallTime=([\d-]+ [\d:]+)"),
            ("lastUpdateTime", r"lastUpdateTime=([\d-]+ [\d:]+)"),
            ("lastTimeUsed", r"lastTimeUsed=([\d-]+ [\d:]+)"),
            ("uid", r"userId=(\d+)"),
        ):
            if key in current:
                continue
            found = re.search(pattern, line)
            if found:
                current[key] = found.group(1)
        if "flags" not in current and re.search(r"\bflags=\[", line):
            current["flags"] = line
        if "pkgFlags" not in current and "pkgFlags=[" in line:
            current["pkgFlags"] = line
        if "enabled" not in current and re.match(r"enabled=(true|false)", line):
            current["enabled"] = line.split("=", 1)[1].strip()
    return packages


def parse_pm_list_packages(text: str) -> list[tuple[str, str, bool]]:
    """Разобрать ``pm list packages -f``: [(путь к APK, пакет, системный?)].

    Строки выглядят так::

        package:/system/priv-app/Settings/Settings.apk=com.android.settings
        package:/data/app/~~xyz==/com.example-1/base.apk=com.example

    ``-s`` (только системные) и ``-3`` (только пользовательские) в одном
    вызове не применяются, поэтому системность определяется по пути.
    """
    result: list[tuple[str, str, bool]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        body = line[len("package:"):]
        if "=" not in body:
            continue
        # Делим по ПОСЛЕДНЕМУ «=»: путь установки Android содержит «==»
        # (``/data/app/~~8Hf2==/com.whatsapp-1/base.apk=com.whatsapp``), и
        # разбор по первому «=» ломал имя пакета.
        path, _, package = body.rpartition("=")
        package = package.strip()
        if not package:
            continue
        is_system = path.startswith(("/system", "/vendor", "/product", "/apex"))
        result.append((path.strip(), package, is_system))
    return result


def apk_path_of(code_path: str, package: str) -> str:
    """Привести ``codePath`` из dumpsys к пути APK.

    ``codePath`` — это **каталог** установки (``/data/app/~~xyz==/com.example-1``),
    а нам нужен файл ``base.apk`` внутри него. Уже готовый путь к ``.apk``
    возвращается как есть. Разделённые APK (split) не поддерживаются: там
    иконка и название обычно лежат в base.
    """
    path = (code_path or "").strip()
    if not path:
        return ""
    if path.endswith(".apk"):
        return path
    return path.rstrip("/") + "/base.apk"


def parse_diskstats(text: str) -> dict[str, dict[str, int]]:
    """Разобрать ``dumpsys diskstats`` и получить размеры по пакетам.

    Внутри лежит строка ``packageSizes`` вида ``pkg1,pkg2,...`` и
    ``packageDataSizes`` — сжатый (zlib) и закодированный base64 массив
    ``int64`` в порядке пакетов. Формат недокументирован, поэтому любые
    ошибки разбора возвращают пустой словарь, а не «нули, похожие на правду».
    """
    lines = {}
    for raw in text.splitlines():
        if ":" in raw:
            key, _, value = raw.partition(":")
            lines[key.strip()] = value.strip()
    names = lines.get("packageSizes") or ""
    encoded = lines.get("packageDataSizes") or ""
    cache_encoded = lines.get("cacheSizes") or ""
    if not names or not encoded:
        return {}

    def _decode(payload: str) -> list[int]:
        import struct

        try:
            raw = zlib.decompress(base64.b64decode(payload.strip()))
        except Exception:  # noqa: BLE001 — формат может отличаться между версиями
            return []
        if len(raw) % 8:
            return []
        return list(struct.unpack(f"<{len(raw) // 8}q", raw))

    data_sizes = _decode(encoded)
    cache_sizes = _decode(cache_encoded) if cache_encoded else []
    packages = [p for p in names.split(",") if p]
    if len(data_sizes) != len(packages):
        return {}
    out: dict[str, dict[str, int]] = {}
    for index, package in enumerate(packages):
        out[package] = {
            "data": max(0, data_sizes[index]),
            "cache": max(0, cache_sizes[index]) if index < len(cache_sizes) else 0,
        }
    return out


def parse_date(value: str) -> int:
    """``2023-05-16 09:31:42`` -> epoch **миллисекунды** (0, если не разобрано)."""
    from datetime import datetime, timezone

    text = (value or "").strip()
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return 0


class AppService:
    """Список приложений с метаданными и иконками."""

    def __init__(self, adb: AdbClient):
        self.adb = adb
        self._icon_cache: dict[str, bytes] = {}

    # ------------------------------------------------------------------ список
    def list_apps(
        self,
        serial: str,
        *,
        with_resources: bool = True,
        cancel_event=None,
    ) -> list[AppInfo]:
        packages = self._dumpsys_packages(serial)
        listed = self._pm_list(serial)
        sizes = self._diskstats(serial)

        apps: dict[str, AppInfo] = {}
        for package, info in packages.items():
            app = AppInfo(package=package)
            app.version_name = info.get("versionName", "")
            app.apk_path = info.get("codePath", "")
            app.installed_at = parse_date(info.get("firstInstallTime", ""))
            app.updated_at = parse_date(info.get("lastUpdateTime", ""))
            app.last_used_at = parse_date(info.get("lastTimeUsed", ""))
            app.uid = info.get("uid", "")
            flags = (info.get("pkgFlags", "") + " " + info.get("flags", "")).upper()
            app.is_system = "SYSTEM" in flags or "/system/" in app.apk_path or "/vendor/" in app.apk_path
            enabled = info.get("enabled", "true")
            app.enabled = enabled.strip().lower() != "false"
            apps[package] = app

        for path, package, is_system in listed:
            app = apps.get(package)
            if app is None:
                app = AppInfo(package=package, is_system=is_system)
                apps[package] = app
            if not app.apk_path:
                app.apk_path = path
            app.apk_path = apk_path_of(app.apk_path, package)
            app.is_system = app.is_system or is_system

        for app in apps.values():
            app.apk_path = apk_path_of(app.apk_path, app.package)

        for package, entry in sizes.items():
            app = apps.get(package)
            if app is None:
                app = AppInfo(package=package)
                apps[package] = app
            app.data_size = entry.get("data", 0)
            app.cache_size = entry.get("cache", 0)
            # Android считает «данные» включая кэш; в интерфейсе они разделены.
            app.data_size = max(0, app.data_size - app.cache_size)

        # Размер APK: один `stat` на пакет — дорого при 300 приложениях,
        # поэтому считаем на устройстве одной командой.
        self._fill_apk_sizes(serial, list(apps.values()), cancel_event=cancel_event)

        for app in apps.values():
            app.category_key, app.category_label = category_for(app.package, is_system=app.is_system)
            app.critical = app.package in CRITICAL_PACKAGES or app.category_key == "system"
            if not app.installed_at:
                app.notes.append("Дата установки недоступна на этой прошивке")
            if not app.last_used_at:
                app.notes.append("Время последнего запуска недоступно (Android 10+ часто скрывает)")

        # Пользовательские приложения — первыми и по убыванию размера:
        # именно их чаще всего ищут, чтобы освободить место.
        result = sorted(apps.values(), key=lambda a: (a.is_system, -a.total_size, a.package))

        if with_resources:
            # Читаем APK один раз и достаём из него и название, и иконку.
            # Ограничение: не больше MAX_RESOURCE_APPS пакетов за раз, иначе
            # список из 300 приложений на медленном телефоне будет собираться
            # минутами. Остальным остаётся имя пакета и заглушка иконки.
            budget = MAX_RESOURCE_APPS
            for app in result:
                if cancel_event is not None and cancel_event.is_set():
                    break
                if not app.apk_path:
                    continue
                if budget <= 0:
                    app.notes.append("Ресурсы APK не читались: исчерпан лимит на список")
                    continue
                budget -= 1
                try:
                    self._load_apk_details(serial, app)
                except (CommandError, CommandTimeout, CommandCancelled, ResourceTableError):
                    continue
        return result

    def _load_apk_details(self, serial: str, app: AppInfo) -> None:
        """Прочитать название и иконку приложения из его APK одним запросом."""
        data = self._read_apk_part(serial, app.apk_path, max_bytes=APK_READ_BYTES)
        resources = ApkResources.from_apk_intro(data)
        label = resources.application_label()
        if label:
            app.label = label
        icon = resources.icon_bytes()
        if icon:
            self._icon_cache[app.package] = icon
            app.icon_available = True

    def _dumpsys_packages(self, serial: str) -> dict[str, dict[str, str]]:
        try:
            res = self.adb.shell(serial, "dumpsys package packages", timeout=120)
        except (CommandError, CommandTimeout):
            return {}
        if not res.ok or "Package [" not in res.stdout:
            return {}
        return parse_dumpsys_packages(res.stdout)

    def _pm_list(self, serial: str) -> list[tuple[str, str, bool]]:
        try:
            res = self.adb.shell(serial, "pm list packages -f", timeout=120)
        except (CommandError, CommandTimeout):
            return []
        if not res.ok:
            return []
        return parse_pm_list_packages(res.stdout)

    def _diskstats(self, serial: str) -> dict[str, dict[str, int]]:
        try:
            res = self.adb.shell(serial, "dumpsys diskstats", timeout=60)
        except (CommandError, CommandTimeout):
            return {}
        if not res.ok:
            return {}
        return parse_diskstats(res.stdout)

    def _fill_apk_sizes(self, serial: str, apps: list[AppInfo], *, cancel_event=None) -> None:
        paths = [a.apk_path for a in apps if a.apk_path]
        if not paths:
            return
        # Одна команда на все файлы: `stat -c %s %n` печатает размер и путь.
        quoted = " ".join(quote_remote_path(p) for p in paths)
        command = f"stat -c '%s|%n' {quoted} 2>/dev/null"
        try:
            res = self.adb.shell(serial, command, timeout=120, cancel_event=cancel_event)
        except (CommandError, CommandTimeout, CommandCancelled):
            return
        by_path = {a.apk_path: a for a in apps if a.apk_path}
        for line in res.stdout.splitlines():
            if "|" not in line:
                continue
            size_raw, _, path = line.partition("|")
            try:
                size = int(size_raw.strip())
            except ValueError:
                continue
            app = by_path.get(path.strip())
            if app is not None:
                app.apk_size = size

    # ------------------------------------------------------------ названия
    def read_label(self, serial: str, app: AppInfo) -> str:
        """Прочитать настоящее название приложения из ресурсов APK."""
        if not app.apk_path:
            return ""
        try:
            data = self._read_apk_part(serial, app.apk_path, max_bytes=6 * 1024 * 1024)
            resources = ApkResources.from_apk_intro(data)
            label = resources.application_label()
            if label:
                app.label = label
            return label
        except (ResourceTableError, CommandError, CommandTimeout):
            return ""

    # ------------------------------------------------------------- иконки
    def icon(self, serial: str, app: AppInfo, *, max_bytes: int = 3 * 1024 * 1024) -> bytes | None:
        """Вернуть PNG/WebP иконку приложения из его APK (или None)."""
        if app.package in self._icon_cache:
            return self._icon_cache[app.package]
        if not app.apk_path:
            return None
        try:
            data = self._read_apk_part(serial, app.apk_path, max_bytes=8 * 1024 * 1024)
            resources = ApkResources.from_apk_intro(data)
            raw = resources.icon_bytes()
        except (ResourceTableError, CommandError, CommandTimeout):
            return None
        if not raw:
            return None
        self._icon_cache[app.package] = raw
        return raw

    def _read_apk_part(self, serial: str, apk_path: str, *, max_bytes: int) -> bytes:
        """Прочитать начало APK: zip-оглавление лежит в конце, но ресурсы — в начале.

        ``head -c`` по APK достаточен, чтобы найти ``resources.arsc`` и файлы
        иконок, потому что zip-архивы APK читаются по локальным заголовкам.
        Ограничение размера защищает память на больших играх.
        """
        quoted = quote_remote_path(apk_path)
        rc, data, err = self.adb.run_binary(
            ["-s", serial, "exec-out", f"head -c {max_bytes} {quoted} 2>/dev/null"],
            timeout=90,
            max_bytes=max_bytes + 8192,
        )
        if not data:
            raise CommandError(err.strip() or "APK пуст или недоступен")
        return data


__all__ = [
    "AppInfo",
    "AppService",
    "CRITICAL_PACKAGES",
    "apk_path_of",
    "category_for",
    "parse_date",
    "parse_diskstats",
    "parse_dumpsys_packages",
    "parse_pm_list_packages",
]
