"""Правила отнесения файлов к категориям и оценки осторожности.

Модуль намеренно чистый: на вход — список :class:`RemoteFile` и настройки,
на выход — :class:`ScanItem` с явными причинами попадания в результаты.
Крупный или старый файл сам по себе мусором не считается — SparkTech
описывает факт и оставляет решение пользователю.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import RemoteFile, ScanItem, RISK_ORDER

ARCHIVE_EXT = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst",
    ".iso", ".cab", ".lz4", ".z", ".tar.gz", ".tbz2", ".apk.bak",
}

APK_EXT = {".apk", ".apks", ".xapk", ".apkm", ".aab", ".apk.1"}

TEMP_EXT = {".tmp", ".temp", ".part", ".partial", ".crdownload", ".download", ".log", ".bak"}

TEMP_NAME_HINTS = ("cache", ".trashed", "thumbnails", "tmp", "temp")

DOWNLOAD_DIR_MARKERS = (
    "/download", "/downloads", "/загрузки", "/browser",
    "/telegram/telegram documents", "/whatsapp/media/whatsapp documents",
    "/bluetooth", "/movie", "/movies", "/podcasts",
)

# Каталоги, где файлы почти всегда имеют ценность для пользователя.
SENSITIVE_DIR_MARKERS = (
    "/dcim/", "/camera/", "/pictures/", "/movies/", "/music/",
    "/documents/", "/whatsapp/media/whatsapp images/",
    "/whatsapp/media/whatsapp video/", "/telegram/telegram images/",
    "/telegram/telegram video/", "/recordings/", "/voice recorder/",
    "/android/media/", "/signal/", "/viber media/", "/screenshots/",
)

# Области, где мы вообще не формируем рекомендации об удалении.
PROTECTED_DIR_MARKERS = (
    "/android/data/", "/android/obb/", "/android/media/", "/.estrongs/",
    "/dcim/.thumbnails/", "/lost.dir/", "/android/",
)

MIN_DUPLICATE_SIZE = 1024 * 1024  # дубликаты меньше 1 МиБ не анализируем


@dataclass(frozen=True)
class Settings:
    big_file_threshold: int = 100 * 1024 * 1024
    min_size: int = 0
    include_apk: bool = True
    include_archives: bool = True
    include_temp: bool = True
    include_downloads: bool = True
    find_duplicates: bool = True
    duplicate_min_size: int = MIN_DUPLICATE_SIZE
    full_hash: bool = False
    max_items: int = 5000
    # Показывать превью (читает файлы с телефона). Отключается, если не
    # нужно нагружать устройство или кабель медленный.
    show_previews: bool = True

    def to_dict(self) -> dict:
        return {
            "big_file_threshold": self.big_file_threshold,
            "min_size": self.min_size,
            "include_apk": self.include_apk,
            "include_archives": self.include_archives,
            "include_temp": self.include_temp,
            "include_downloads": self.include_downloads,
            "find_duplicates": self.find_duplicates,
            "duplicate_min_size": self.duplicate_min_size,
            "full_hash": self.full_hash,
            "max_items": self.max_items,
            "show_previews": self.show_previews,
        }

    @classmethod
    def from_payload(cls, payload: dict | None) -> "Settings":
        """Аккуратно собрать настройки из запроса пользователя."""
        payload = payload or {}
        d = cls()

        def as_int(key: str, default: int, lo: int, hi: int) -> int:
            try:
                value = int(payload.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, value))

        def as_bool(key: str, default: bool) -> bool:
            value = payload.get(key, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("1", "true", "on", "yes")
            return default

        return cls(
            big_file_threshold=as_int("big_file_threshold", d.big_file_threshold, 1024 * 1024, 100 * 1024 ** 3),
            min_size=as_int("min_size", 0, 0, 100 * 1024 ** 3),
            include_apk=as_bool("include_apk", d.include_apk),
            include_archives=as_bool("include_archives", d.include_archives),
            include_temp=as_bool("include_temp", d.include_temp),
            include_downloads=as_bool("include_downloads", d.include_downloads),
            find_duplicates=as_bool("find_duplicates", d.find_duplicates),
            duplicate_min_size=as_int("duplicate_min_size", d.duplicate_min_size, 0, 100 * 1024 ** 3),
            full_hash=as_bool("full_hash", d.full_hash),
            max_items=max(100, min(20000, as_int("max_items", d.max_items, 100, 20000))),
            show_previews=as_bool("show_previews", d.show_previews),
        )


MEDIA_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif", ".avif", ".dng"}
MEDIA_VIDEO_EXT = {".mp4", ".mkv", ".mov", ".3gp", ".webm", ".m4v", ".avi", ".ts", ".flv"}

# Закрытые для рекомендаций каталоги, внутри которых всё же лежат обычные
# пользовательские медиа (DCIM, Pictures, Movies, Screenshots).
USER_MEDIA_MARKERS = (
    "/dcim/", "/dcim100", "/dcim200", "/camera/", "/pictures/", "/movies/",
    "/screenshots/", "/screen recordings/", "/screenrecords/", "/videos/",
)


def _in_downloads(path: str) -> bool:
    """Путь относится к «Загрузкам»/мессенджерам (download_kind возвращает bool)."""
    return bool(download_kind(path))


def _is_user_media(path: str) -> bool:
    """Файл — обычное фото/видео в пользовательском каталоге камеры/скриншотов."""
    low = _low(path)
    if "/.thumbnails/" in low or "/.trashed/" in low:
        return False
    ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path.rsplit("/", 1)[-1] else ""
    if ext not in MEDIA_IMAGE_EXT and ext not in MEDIA_VIDEO_EXT:
        return False
    return any(marker in low for marker in USER_MEDIA_MARKERS)


def media_kind(path: str) -> str | None:
    """Вид медиафайла: ``image``, ``video`` или None."""
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    ext = "." + name.rsplit(".", 1)[-1].lower()
    if ext in MEDIA_IMAGE_EXT:
        return "image"
    if ext in MEDIA_VIDEO_EXT:
        return "video"
    return None


def _low(path: str) -> str:
    return path.lower()


def is_in_protected_dir(path: str) -> bool:
    low = _low(path)
    return any(marker in low for marker in PROTECTED_DIR_MARKERS)


def archive_kind(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in ARCHIVE_EXT)


def apk_kind(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in APK_EXT)


def temp_kind(file: RemoteFile) -> bool:
    low_name = file.name.lower()
    if any(low_name.endswith(ext) for ext in TEMP_EXT):
        return True
    # Каталоги с явно временным назначением (cache/tmp/thumbnails) в
    # пользовательской памяти.
    if any(hint in _low(file.directory) for hint in TEMP_NAME_HINTS):
        # Но не считаем временными файлы в «cache»-каталогах фото/аудио,
        # где лежат, например, ehcache от приложений мессенджеров.
        return True
    return False


def download_kind(path: str) -> bool:
    low = _low(path)
    return any(marker in low for marker in DOWNLOAD_DIR_MARKERS)


def classify_file(
    file: RemoteFile, settings: Settings, skip_reason: dict[str, str] | None = None
) -> ScanItem | None:
    """Отнести файл к категориям. ``None`` — файл не интересен.

    Если передан ``skip_reason``, в него записывается причина, по которой
    файл не попал в результаты (для честной статистики покрытия).
    """
    reasons: list[str] = []
    categories: list[str] = []
    warnings: list[str] = []
    risk = "low"
    low = _low(file.path)

    # Смена/скриншоты в DCIM — это пользовательские медиа, а не закрытые
    # данные. Без этого исключения фото и видео из DCIM вообще не попадали
    # в список, и посмотреть их перед удалением было нельзя.
    if is_in_protected_dir(file.path) and not _is_user_media(file.path):
        # Закрытые системой каталоги: не предлагаем удаление вовсе.
        if skip_reason is not None:
            skip_reason["reason"] = "protected"
        # Исключение — OBB-файлы игр: они большого размера и часто интересны
        # пользователю. Удалить их SparkTech не даст, но показать обязан.
        if "obb" in _low(file.path) and file.size >= settings.big_file_threshold:
            return ScanItem(
                file=file,
                categories=["big"],
                reasons=[
                    "Файл данных игры или приложения в Android/obb, больше порога крупного файла."
                ],
                risk="high",
                extra_warnings=[
                    "Android/obb защищён системой: SparkTech не удаляет такие файлы. "
                    "Освободить место можно только удалением приложения в настройках Android.",
                ],
            )
        return None
    if settings.min_size and file.size < settings.min_size:
        if skip_reason is not None:
            skip_reason["reason"] = "small"
        return None

    # Приоритет политики: защищённые каталоги > пользовательские медиа и
    # документы > конкретные категории. Файл в DCIM/Pictures/Documents не
    # получает «низкую» осторожность только из-за расширения.
    for marker in SENSITIVE_DIR_MARKERS:
        if marker in low:
            warnings.append(
                "Файл лежит в каталоге пользовательских медиа или документов — такие данные "
                "легко потерять безвозвратно и трудно восстановить."
            )
            risk = "high"
            break

    if file.size >= settings.big_file_threshold:
        categories.append("big")
        reasons.append(
            f"Размер {_fmt(file.size)} превышает порог крупного файла "
            f"({_fmt(settings.big_file_threshold)}). Сам по себе размер не делает файл мусором."
        )
        risk = max(risk, "medium", key=RISK_ORDER.get)

    media = media_kind(file.path)
    if media is not None:
        # Фото и видео из загрузок, мессенджеров и камеры: их чаще всего и
        # хотят проверить перед удалением. Категория отдельная от «Крупный».
        # Показываем медиа из «Загрузок», мессенджеров и камеры. Мелкий
        # ролик в Documents — это документ пользователя, его не трогаем.
        if _in_downloads(file.path) or _is_user_media(file.path):
            categories.append("photo" if media == "image" else "video")
            reasons.append(
                "Фото" if media == "image" else "Видео"
            )
            reasons[-1] += (
                " в пользовательской памяти. SparkTech показывает превью — "
                "посмотрите файл перед удалением."
            )
            risk = max(risk, "medium", key=RISK_ORDER.get)

    if settings.include_downloads and download_kind(file.path):
        categories.append("download")
        reasons.append("Файл находится в папке загрузок или в каталоге, куда приложения сохраняют полученные файлы.")
        risk = max(risk, "medium", key=RISK_ORDER.get)

    if settings.include_archives and archive_kind(file.name):
        categories.append("archive")
        reasons.append("Архив: обычно это скачанный или созданный вручную набор файлов.")
        risk = max(risk, "medium", key=RISK_ORDER.get)

    if settings.include_apk and apk_kind(file.name):
        categories.append("apk")
        reasons.append("Установочный пакет Android: нужен только для установки или переустановки приложения.")
        risk = max(risk, "medium", key=RISK_ORDER.get)

    if settings.include_temp and temp_kind(file):
        categories.append("temp")
        reasons.append("Имя файла или каталога похоже на временные данные (cache/tmp/temp/log/part).")
        # В чужих каталогах «временный» файл может быть рабочим файлом приложения.
        if not any(hint in low for hint in ("/cache/", "/tmp/", "/temp/")):
            warnings.append(
                "Расширение временного файла не гарантирует, что он не нужен: некоторые "
                "приложения используют .tmp/.log как рабочие файлы."
            )
            risk = max(risk, "high", key=RISK_ORDER.get)

    if not categories:
        if skip_reason is not None:
            skip_reason["reason"] = "other"
        return None

    if file.size == 0:
        warnings.append("Файл пустой (0 байт).")
    if file.mtime and _older_than_years(file.mtime, 2) and "big" in categories:
        warnings.append("Файл не изменялся более двух лет. Это лишь справочная информация, а не признак мусора.")

    return ScanItem(
        file=file,
        categories=categories,
        reasons=reasons,
        risk=risk,
        extra_warnings=warnings,
    )


def classify_all(
    files: Iterable[RemoteFile], settings: Settings, skip_stats: dict[str, int] | None = None
) -> list[ScanItem]:
    """Классифицировать все файлы; при желании — посчитать пропуски по причинам."""
    items: list[ScanItem] = []
    for file in files:
        reason: dict[str, str] = {}
        item = classify_file(file, settings, skip_reason=reason)
        if item is not None:
            items.append(item)
        elif skip_stats is not None:
            key = reason.get("reason", "other")
            skip_stats[key] = skip_stats.get(key, 0) + 1
    return items


# ------------------------------------------------------------------ дубликаты


@dataclass
class DuplicateCandidate:
    """Группа файлов одинакового размера, которых стоит хешировать."""

    files: list[RemoteFile]
    hash_kind: str


def duplicate_candidates(
    files: Iterable[RemoteFile], settings: Settings
) -> list[DuplicateCandidate]:
    """Найти группы одинакового размера для последующей проверки хешем.

    Дубликаты ищутся только среди файлов не меньше ``duplicate_min_size``:
    для мелких файлов цена ошибки и польза от удаления несоизмеримы.
    """
    if not settings.find_duplicates:
        return []
    by_size: dict[int, list[RemoteFile]] = {}
    for file in files:
        if file.size < max(settings.duplicate_min_size, 1024):
            continue
        if is_in_protected_dir(file.path) and "obb" not in _low(file.path):
            continue
        by_size.setdefault(file.size, []).append(file)
    kind = "full" if settings.full_hash else "partial"
    return [
        DuplicateCandidate(files=sorted(group, key=lambda f: f.path), hash_kind=kind)
        for size, group in by_size.items()
        if len(group) > 1
    ]


def apply_duplicates(
    items: list[ScanItem],
    hashes: dict[str, str],
    hash_kind: str,
    all_files: list[RemoteFile] | None = None,
) -> list[ScanItem]:
    """Пометить возможные дубликаты по вычисленным хешам.

    Одинаковый хеш содержимого — основание предложить файл как возможную
    копию, но не основание удалять автоматически. «Оригиналом» считается
    файл с наименьшим числом причин для удаления (затем первый по алфавиту).

    Если передан ``all_files`` (все увиденные файлы), копии, которые не
    попали в другие категории, всё равно добавляются в список — иначе
    пользователь не увидит дубликат, спрятанный в медиакаталоге.
    """
    by_path = {item.path: item for item in items}
    known: dict[str, RemoteFile] = {f.path: f for f in (all_files or [])}
    for item in items:
        known.setdefault(item.path, item.file)

    groups: dict[str, list[str]] = {}
    for path, digest in hashes.items():
        if digest:
            groups.setdefault(digest, []).append(path)

    how = "полного" if hash_kind == "full" else "частичного (первые 64 КиБ)"
    for digest, paths in groups.items():
        if len(paths) < 2:
            continue
        entries: list[ScanItem] = []
        for path in paths:
            item = by_path.get(path)
            if item is None and path in known:
                settings_stub = ScanItem(file=known[path], categories=[], reasons=[], risk="medium")
                items.append(settings_stub)
                by_path[path] = settings_stub
                item = settings_stub
            if item is not None:
                entries.append(item)
        if len(entries) < 2:
            continue
        entries.sort(key=lambda i: (len(i.categories), i.path))
        original = entries[0]
        if "original" not in original.categories:
            original.categories.append("original")
            original.reasons.append(
                "У этого файла есть копии на устройстве (см. помеченные «Возможный дубликат»). "
                "SparkTech не предлагает удалять его автоматически."
            )
        for dup in entries[1:]:
            if "duplicate" not in dup.categories:
                dup.categories.append("duplicate")
            dup.duplicate_of = original.path
            dup.hash_kind = hash_kind
            dup.reasons.append(
                f"Совпадение {how} хеша содержимого с файлом {original.path} "
                f"(размер {_fmt(dup.size)})."
            )
            if hash_kind == "partial":
                dup.extra_warnings.append(
                    "Дубликат определён по частичному хешу — файлы могут различаться "
                    "во второй половине. Включите полное хеширование для точной проверки."
                )
            else:
                dup.extra_warnings.append(
                    "Совпадение полного хеша с высокой вероятностью означает идентичное содержимое, "
                    "но файл всё равно не удаляется автоматически."
                )
            dup.risk = max(dup.risk, "medium", key=RISK_ORDER.get)
    return items


# ------------------------------------------------------------------ сортировка


SORT_KEYS = {
    "size": lambda i: i.size,
    "name": lambda i: i.file.name.lower(),
    "path": lambda i: i.path.lower(),
    "date": lambda i: i.file.mtime,
    "risk": lambda i: RISK_ORDER.get(i.risk, 1),
    "category": lambda i: i.category_key,
}


def filter_items(
    items: list[ScanItem],
    *,
    query: str = "",
    categories: Iterable[str] | None = None,
    risks: Iterable[str] | None = None,
    min_size: int = 0,
    sort: str = "size",
    descending: bool = True,
    limit: int | None = None,
) -> list[ScanItem]:
    """Фильтрация и сортировка результатов (используется и сервером, и тестами)."""
    cats = set(categories or [])
    risk_set = set(risks or [])
    q = (query or "").strip().lower()
    result = []
    for item in items:
        if cats and not (set(item.categories) & cats):
            continue
        if risk_set and item.risk not in risk_set:
            continue
        if min_size and item.size < min_size:
            continue
        if q and q not in item.path.lower():
            continue
        result.append(item)
    key = SORT_KEYS.get(sort, SORT_KEYS["size"])
    result.sort(key=key, reverse=descending)
    if limit is not None:
        result = result[:limit]
    return result


def _fmt(value: int) -> str:
    units = ("Б", "КиБ", "МиБ", "ГиБ")
    v = float(value)
    for unit in units:
        if v < 1024 or unit == "ГиБ":
            return f"{v:.0f} {unit}" if unit == "Б" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} ГиБ"


def _older_than_years(mtime: int, years: int) -> bool:
    import time

    return (time.time() - mtime) > years * 365 * 24 * 3600
