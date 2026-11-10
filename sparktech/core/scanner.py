"""Сканирование доступной пользовательской памяти через ADB.

Что делает SparkTech:

* обходит только те каталоги, которые реально видны ``adb shell`` без
  root и специальных привилегий;
* получает список файлов одной командой ``find`` с разделителем полей,
  который невозможно встретить в имени файла;
* честно перечисляет области, которые не удалось прочитать.

Чего SparkTech не делает: не читает ``/data/data``, не «видит» кэш
приложений, не обходит ограничения Android 11+ (scoped storage) и не
притворяется, что просканировал всю внутреннюю память.
"""

from __future__ import annotations

import shlex
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from . import classify
from .adb import AdbClient
from .classify import Settings
from .models import RemoteFile, ScanArea, ScanItem, ScanResult, make_scan_id
from .process import CommandCancelled, CommandError, CommandTimeout
from .remote_path import normalize_remote, quote_remote_path, strip_sdcard_prefix

# Пользовательские каталоги, которые имеет смысл обходить.
# Все они доступны обычному приложению adbd без root (с оговорками Android 11+).
SCAN_ROOTS = [
    "/storage/emulated/0/Download",
    "/storage/emulated/0/Downloads",
    "/storage/emulated/0/DCIM",
    "/storage/emulated/0/Pictures",
    "/storage/emulated/0/Movies",
    "/storage/emulated/0/Music",
    "/storage/emulated/0/Documents",
    "/storage/emulated/0/Android/media",
    # /Android/obb добавляется только при include_obb: файлы видны, но удаление запрещено.
    "/storage/emulated/0/Bluetooth",
    "/storage/emulated/0/Ringtones",
    "/storage/emulated/0/Alarms",
    "/storage/emulated/0/Notifications",
    "/storage/emulated/0/Podcasts",
    "/storage/emulated/0/Recordings",
    "/storage/emulated/0/Telegram",
    "/storage/emulated/0/WhatsApp",
    "/storage/emulated/0/Movies",
]

# Каталоги, в которые не заходим: там тысячи служебных файлов и/или
# ограничения доступа. Сообщаем о них отдельно.
EXCLUDED_SUBDIRS = [
    "Android/data",
    "Android/obb/.*",
    ".thumbnails",
    ".trashed",
    "LOST.DIR",
]

FIELD_SEP = "\x1f"
FIND_TIMEOUT = 15 * 60
IDLE_TIMEOUT = 120

ProgressCallback = Callable[[str, dict], None]


def default_progress(stage: str, **data) -> dict:
    return {"stage": stage, **data}


@dataclass
class ScanOptions:
    settings: Settings = field(default_factory=Settings)
    include_obb: bool = False
    extra_roots: list[str] = field(default_factory=list)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Scanner:
    def __init__(self, adb: AdbClient):
        self.adb = adb

    # --------------------------------------------------------------- storage

    def storage_stats(self, serial: str) -> tuple[int | None, int | None]:
        """Свободно/всего на основной пользовательской памяти (в байтах)."""
        res = self.adb.shell(serial, "df -k /storage/emulated/0 2>/dev/null || df -k /data", timeout=20)
        if not res.ok:
            return None, None
        lines = [l for l in res.stdout.splitlines() if l.strip()]
        for line in reversed(lines):
            parts = line.split()
            if len(parts) >= 4 and parts[0].startswith("/"):
                try:
                    total_k = int(parts[1])
                    free_k = int(parts[3])
                except ValueError:
                    continue
                return total_k * 1024, free_k * 1024
        return None, None

    # ------------------------------------------------------------- области

    def existing_roots(self, serial: str, extra_roots: Iterable[str] = ()) -> list[str]:
        """Оставить только существующие каталоги (одним вызовом оболочки)."""
        roots = list(dict.fromkeys([*SCAN_ROOTS, *extra_roots]))
        script = " ; ".join(
            f"if [ -e {quote_remote_path(r)} ]; then echo {quote_remote_path(r)}; fi"
            for r in roots
        )
        res = self.adb.shell(serial, script, timeout=30)
        if not res.ok:
            return []
        found = [normalize_remote(l.strip()) for l in res.stdout.splitlines() if l.strip().startswith("/")]
        return [r for r in roots if r in found]

    # ------------------------------------------------------------- сканирование

    def scan(
        self,
        serial: str,
        *,
        model: str | None = None,
        options: ScanOptions | None = None,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ScanResult:
        options = options or ScanOptions()
        settings = options.settings
        notify = progress or (lambda stage, data: None)

        result = ScanResult(
            serial=serial,
            model=model,
            started_at=_utc_now(),
            scan_id=make_scan_id(serial, _utc_now()),
        )
        result.storage_total, result.storage_free = self.storage_stats(serial)

        notify("discover", {"message": "Поиск доступных каталогов на устройстве"})
        extra = list(options.extra_roots)
        if options.include_obb:
            # OBB доступен для чтения метаданных, но удаление этих путей
            # запрещено (см. remote_path.PROTECTED_PREFIXES).
            extra.append("/storage/emulated/0/Android/obb")
        roots = self.existing_roots(serial, extra)
        if not roots:
            raise CommandError(
                "Не удалось получить доступ к пользовательским каталогам. "
                "Проверьте, что устройство разблокировано и отладка разрешена."
            )
        for extra_root in options.extra_roots:
            normalized = normalize_remote(extra_root)
            if normalized and normalized not in roots:
                roots.append(normalized)

        files_by_path: dict[str, RemoteFile] = {}
        # Все увиденные файлы (включая закрытые каталоги) — нужны, чтобы честно
        # посчитать пропуски и найти дубликаты в защищённых областях.
        all_seen: dict[str, RemoteFile] = {}
        total_seen = 0
        odd_names = 0
        started = time.monotonic()

        for index, root in enumerate(roots, start=1):
            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True
                break
            notify(
                "scan",
                {
                    "message": f"Чтение {root} ({index} из {len(roots)})",
                    "root": root,
                    "root_index": index,
                    "roots_total": len(roots),
                    "files": len(files_by_path),
                    "elapsed": round(time.monotonic() - started, 1),
                },
            )
            area, found = self._scan_root(
                serial, root, cancel_event=cancel_event, notify=notify,
                files_so_far=len(files_by_path), include_obb=options.include_obb,
            )
            result.areas.append(area)
            if result.cancelled:
                result.cancelled = True
            for file in found:
                total_seen += 1
                if file.path in all_seen:
                    continue
                all_seen[file.path] = file
                # OBB-файлы передаём в анализ: они показываются (но не удаляются).
                if not classify.is_in_protected_dir(file.path) or "/obb/" in file.path.lower():
                    files_by_path[file.path] = file
            odd_names += getattr(area, "_odd", 0) if hasattr(area, "_odd") else 0

        result.total_files_seen = total_seen
        result.skipped_odd_names = odd_names

        notify("classify", {"message": "Анализ найденных файлов", "files": len(files_by_path)})
        items = classify.classify_all(files_by_path.values(), settings, skip_stats=result.skipped)
        items.sort(key=lambda i: i.size, reverse=True)
        if len(items) > settings.max_items:
            result.warnings.append(
                f"Найдено больше {settings.max_items} подходящих файлов; показаны самые крупные. "
                "Увеличьте лимит или сузьте область сканирования."
            )
            items = items[: settings.max_items]

        if settings.find_duplicates and not result.cancelled:
            items = self._mark_duplicates(
                serial, items, list(all_seen.values()), settings, notify, cancel_event
            )

        result.items = items
        result.finished_at = _utc_now()
        result.warnings.extend(self._coverage_warnings(result, roots, settings))
        notify("done", {"message": "Сканирование завершено", "count": len(items)})
        return result

    def _scan_root(
        self,
        serial: str,
        root: str,
        *,
        cancel_event: threading.Event | None,
        notify: ProgressCallback,
        files_so_far: int,
        include_obb: bool = False,
    ) -> tuple[ScanArea, list[RemoteFile]]:
        quoted = quote_remote_path(root)
        # Разделитель \x1f (unit separator) не встречается в путях Android.
        # -print0 был бы чище, но `adb shell` буферизует поток; построчный
        # вывод с разделителем работает стабильно и на старых adb.
        command = find_command(root, include_obb=include_obb)
        files: list[RemoteFile] = []
        odd = 0
        last_emit = 0.0

        def on_line(line: str) -> None:
            nonlocal last_emit
            parsed = parse_find_line(line, root)
            if parsed is None:
                return
            files.append(parsed)
            now = time.monotonic()
            if now - last_emit > 0.4:
                last_emit = now
                notify(
                    "scan",
                    {
                        "message": f"Чтение {root}",
                        "root": root,
                        "root_files": len(files),
                        "files": files_so_far + len(files),
                    },
                )

        try:
            res = self.adb.run_streaming(
                [
                    "-s", serial, "shell", command,
                ],
                on_line=on_line,
                timeout=FIND_TIMEOUT,
                idle_timeout=IDLE_TIMEOUT,
                cancel_event=cancel_event,
            )
            stderr_missing = "No such file" in (res.stdout + res.stderr)
            area = ScanArea(path=root, status="scanned", files=len(files))
            if stderr_missing and not files:
                area.status = "missing"
            return area, files
        except CommandCancelled:
            return (
                ScanArea(path=root, status="scanned", files=len(files), note="Сканирование прервано"),
                files,
            )
        except CommandTimeout:
            return (
                ScanArea(
                    path=root,
                    status="denied" if not files else "scanned",
                    files=len(files),
                    note="Устройство не ответило вовремя; результаты по этому каталогу неполные",
                ),
                files,
            )
        except CommandError as exc:
            return (
                ScanArea(path=root, status="denied", files=len(files), note=str(exc)),
                files,
            )

    # ------------------------------------------------------------- дубликаты

    def _mark_duplicates(
        self,
        serial: str,
        items: list,
        all_files: list[RemoteFile],
        settings: Settings,
        notify: ProgressCallback,
        cancel_event: threading.Event | None,
    ) -> list:
        # Дубликаты ищем по всем увиденным файлам: защищённый оригинал может
        # лежать, например, в Android/media, а копия — в Загрузках.
        candidates = classify.duplicate_candidates(all_files, settings)
        if not candidates:
            return items

        # Не хешируем больше N файлов: иначе Android «устанет» и операция
        # станет непредсказуемой по времени.
        budget_files = 200
        budget_bytes = 512 * 1024 * 1024
        selected: list[classify.DuplicateCandidate] = []
        used_files = 0
        used_bytes = 0
        for cand in sorted(candidates, key=lambda c: -c.files[0].size):
            need = len(cand.files)
            bytes_needed = cand.files[0].size * need
            if settings.full_hash:
                if used_bytes + bytes_needed > budget_bytes:
                    continue
            if used_files + need > budget_files:
                continue
            selected.append(cand)
            used_files += need
            used_bytes += bytes_needed if settings.full_hash else 0
        if not selected:
            return items
        if len(selected) < len(candidates):
            notify(
                "duplicates",
                {
                    "message": (
                        f"Проверка дубликатов ограничена {used_files} файлами. "
                        "Остальные группы не проверялись."
                    )
                },
            )

        hash_kind = "full" if settings.full_hash else "partial"
        hashes: dict[str, str] = {}
        total = len(selected)
        for idx, cand in enumerate(selected, start=1):
            if cancel_event is not None and cancel_event.is_set():
                break
            notify(
                "duplicates",
                {
                    "message": f"Сравнение содержимого: группа {idx} из {total}",
                    "done": idx - 1,
                    "total": total,
                },
            )
            for file in cand.files:
                if cancel_event is not None and cancel_event.is_set():
                    break
                digest = self._remote_hash(serial, file.path, hash_kind, cancel_event)
                if digest:
                    hashes[file.path] = digest
        notify("duplicates", {"message": "Обработка результатов сравнения", "done": total, "total": total})
        return classify.apply_duplicates(items, hashes, hash_kind, all_files=all_files)

    def _remote_hash(
        self,
        serial: str,
        path: str,
        kind: str,
        cancel_event: threading.Event | None,
    ) -> str | None:
        quoted = quote_remote_path(path)
        if kind == "full":
            cmd = f"sha256sum {quoted} 2>/dev/null | cut -d' ' -f1"
        else:
            cmd = f"head -c 65536 {quoted} 2>/dev/null | sha256sum | cut -d' ' -f1"
        try:
            res = self.adb.shell(serial, cmd, timeout=120, cancel_event=cancel_event)
        except (CommandError, CommandTimeout):
            return None
        digest = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
        if len(digest) < 32:
            return None
        return digest

    # --------------------------------------------------------------- покрытие

    def _coverage_warnings(self, result: ScanResult, roots: list[str], settings: Settings) -> list[str]:
        warnings: list[str] = []
        warnings.append(
            "Проверены только общие пользовательские каталоги. Закрытые данные приложений "
            "(/data/data), системные разделы и кэш приложений недоступны без root и не сканировались."
        )
        if any(a.status == "denied" for a in result.areas):
            warnings.append(
                "Часть каталогов не удалось прочитать (ограничения Android 11+ или права доступа). "
                "Список областей и их статус приведён в отчёте о покрытии."
            )
        if not settings.find_duplicates:
            warnings.append("Поиск дубликатов был отключён в настройках.")
        return warnings


# ------------------------------------------------------------------- парсинг


def parse_find_line(line: str, root: str = "") -> RemoteFile | None:
    """Разобрать строку ``РАЗМЕР\\x1fMTIME\\x1fПУТЬ``.

    Возвращает None для неполных строк (find иногда дописывает ошибки в
    stdout при отсутствии прав).
    """
    parts = line.rstrip("\n").split(FIELD_SEP, 2)
    if len(parts) != 3:
        return None
    size_raw, mtime_raw, path = parts
    path = path.strip()
    if not path.startswith("/"):
        return None
    try:
        size = int(size_raw.strip())
    except ValueError:
        return None
    mtime = 0
    try:
        mtime = int(float(mtime_raw.strip()))
    except ValueError:
        mtime = 0
    path = normalize_remote(path)
    if not path:
        return None
    return RemoteFile(path=path, size=size, mtime=mtime, root=root)


def find_command(root: str, *, include_obb: bool = False) -> str:
    """Собрать команду find с исключениями.

    ``Android/data`` не читаем никогда (закрыт scoped storage и не содержит
    пользовательских файлов). ``Android/obb`` исключается, если пользователь
    не попросил показать файлы игр отдельно — они только для отображения,
    удаление таких путей запрещено.
    """
    exclusions = ["-path '*/Android/data'"]
    if not include_obb:
        exclusions.append("-path '*/Android/obb/*'")
    exclusions += ["-name .thumbnails", "-name .trashed", "-name LOST.DIR"]
    prune = " -o ".join(exclusions)
    return (
        f"find {quote_remote_path(root)} \\( {prune} \\) -prune "
        f"-o -type f -printf '%s{FIELD_SEP}%T@{FIELD_SEP}%p\\n' 2>/dev/null"
    )


def parse_find_script_command(root: str) -> str:
    """Сформировать команду поиска (вынесено для тестов на инъекции)."""
    return (
        f"find {quote_remote_path(root)} -type f "
        f"-printf '%s{FIELD_SEP}%T@{FIELD_SEP}%p\\n' 2>/dev/null"
    )
