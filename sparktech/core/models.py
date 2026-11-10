"""Модели данных SparkTech (не зависят от ADB и от веб-интерфейса)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


CATEGORY_LABELS = {
    "big": "Крупный файл",
    "photo": "Фото",
    "video": "Видео",
    "download": "Загрузки",
    "archive": "Архив",
    "apk": "Установочный пакет",
    "temp": "Временный файл",
    "duplicate": "Возможный дубликат",
    "original": "Оригинал группы копий",
    "other": "Прочее",
}

# Уровни осторожности. Намеренно нет значения «безопасно удалить»:
# SparkTech не берётся решать за пользователя, что является мусором.
RISK_LABELS = {
    "low": "Низкая осторожность",
    "medium": "Средняя осторожность",
    "high": "Высокая осторожность",
}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class RemoteFile:
    """Файл на устройстве, полученный из ``find``/``stat``."""

    path: str
    size: int
    mtime: int  # epoch seconds (0, если не удалось получить)
    root: str = ""

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def directory(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else "/"

    @property
    def extension(self) -> str:
        name = self.name
        if "." not in name:
            return ""
        return "." + name.rsplit(".", 1)[-1].lower()


@dataclass
class ScanItem:
    """Элемент результата сканирования с обоснованием."""

    file: RemoteFile
    categories: list[str]
    reasons: list[str]
    risk: str = "medium"
    extra_warnings: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    hash_kind: str | None = None  # "partial" | "full"

    @property
    def path(self) -> str:
        return self.file.path

    @property
    def size(self) -> int:
        return self.file.size

    @property
    def category_key(self) -> str:
        """Основная категория для группировки в интерфейсе."""
        for key in ("duplicate", "apk", "archive", "temp", "download", "video", "photo", "big"):
            if key in self.categories:
                return key
        return self.categories[0] if self.categories else "other"

    @property
    def category_label(self) -> str:
        return " + ".join(CATEGORY_LABELS.get(c, c) for c in self.categories) or CATEGORY_LABELS["other"]

    def is_deletable(self) -> bool:
        """Может ли SparkTech технически удалить этот путь (без учёта выбора)."""
        from .remote_path import is_safe_deletable

        return is_safe_deletable(self.path)[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.file.name,
            "directory": self.file.directory,
            "size": self.size,
            "mtime": self.file.mtime,
            "extension": self.file.extension,
            "categories": self.categories,
            "category_key": self.category_key,
            "category_label": self.category_label,
            "reasons": self.reasons,
            "risk": self.risk,
            "risk_label": RISK_LABELS.get(self.risk, self.risk),
            "warnings": self.extra_warnings,
            "duplicate_of": self.duplicate_of,
            "hash_kind": self.hash_kind,
            "deletable": self.is_deletable(),
        }


@dataclass
class ScanArea:
    """Область сканирования и её доступность."""

    path: str
    status: str  # "scanned" | "denied" | "missing" | "skipped"
    files: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        labels = {
            "scanned": "Проверено",
            "denied": "Нет доступа",
            "missing": "Отсутствует",
            "skipped": "Пропущено",
        }
        return {
            "path": self.path,
            "status": self.status,
            "status_label": labels.get(self.status, self.status),
            "files": self.files,
            "note": self.note,
        }


@dataclass
class ScanResult:
    serial: str
    model: str | None
    items: list[ScanItem] = field(default_factory=list)
    areas: list[ScanArea] = field(default_factory=list)
    total_files_seen: int = 0
    skipped_odd_names: int = 0
    storage_total: int | None = None
    storage_free: int | None = None
    started_at: str = ""
    finished_at: str = ""
    cancelled: bool = False
    warnings: list[str] = field(default_factory=list)
    scan_id: str = ""
    # Файлы, которые мы видели, но которые не попали в результаты по
    # объективным причинам (закрытые каталоги, мелкий размер, фильтры).
    skipped: dict[str, int] = field(default_factory=lambda: {
        "protected": 0,
        "small": 0,
        "other": 0,
    })

    @property
    def total_size(self) -> int:
        return sum(i.size for i in self.items)

    def summary_by_category(self) -> list[dict[str, Any]]:
        acc: dict[str, dict[str, Any]] = {}
        for item in self.items:
            key = item.category_key
            entry = acc.setdefault(
                key, {"key": key, "label": CATEGORY_LABELS.get(key, key), "count": 0, "size": 0}
            )
            entry["count"] += 1
            entry["size"] += item.size
        return sorted(acc.values(), key=lambda e: e["size"], reverse=True)

    def free_after_delete(self, removed: int) -> dict[str, Any]:
        """Сколько станет свободно, если удалить ``removed`` байт.

        Честная арифметика по данным ``df`` с устройства: свободно сейчас +
        подтверждённый объём выбранных файлов. Если размер свободного места
        неизвестен, возвращается None — лучше «нет данных», чем выдумка.
        """
        current = self.storage_free
        if current is None:
            return {"known": False, "current": None, "after": None, "total": self.storage_total}
        after = current + max(0, int(removed))
        if self.storage_total:
            after = min(after, self.storage_total)
        return {"known": True, "current": current, "after": after, "total": self.storage_total}

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "serial": self.serial,
            "model": self.model,
            "items": [i.to_dict() for i in self.items],
            "areas": [a.to_dict() for a in self.areas],
            "total_files_seen": self.total_files_seen,
            "skipped_odd_names": self.skipped_odd_names,
            "storage_total": self.storage_total,
            "storage_free": self.storage_free,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancelled": self.cancelled,
            "warnings": self.warnings,
            "skipped": dict(self.skipped),
            "total_size": self.total_size,
            "summary": self.summary_by_category(),
            "count": len(self.items),
        }


def make_scan_id(serial: str, started_at: str) -> str:
    raw = f"{serial}|{started_at}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class DeleteResult:
    path: str
    ok: bool
    message: str = ""
    size: int = 0  # подтверждённый размер на момент удаления (0 — неизвестен)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "ok": self.ok, "message": self.message, "size": self.size}


@dataclass
class ActionOutcome:
    action_id: str
    status: str  # success | failed | user_action | error | skipped
    message: str
    details: list[str] = field(default_factory=list)
    reversible: bool = False
    can_undo: bool = False

    def to_dict(self) -> dict[str, Any]:
        labels = {
            "success": "Успешно",
            "failed": "Не выполнено",
            "user_action": "Требуется действие пользователя",
            "error": "Ошибка",
            "skipped": "Пропущено",
        }
        return {
            "action_id": self.action_id,
            "status": self.status,
            "status_label": labels.get(self.status, self.status),
            "message": self.message,
            "details": self.details,
            "reversible": self.reversible,
            "can_undo": self.can_undo,
        }


def human_bytes(value: int | float | None) -> str:
    """Форматирование размера для журналов и отчётов (в UI форматирует JS)."""
    if value is None:
        return "—"
    value = float(value)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if abs(value) < 1024 or unit == "ТиБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТиБ"
