"""Открытие файлов и папок средствами операционной системы.

Зачем отдельный модуль: SparkTech — программа для Windows, и пользователь
ожидает привычного поведения: файл открывается в **своём** плеере или
программе по умолчанию, а папка — в проводнике. Браузерное окно программы
для этого не подходит, поэтому здесь только системные механизмы:

* Windows — ``os.startfile`` (то же, что двойной клик в проводнике) и
  ``explorer /select,`` для показа файла в папке с выделением;
* macOS — ``open`` и ``open -R``;
* Linux — ``xdg-open`` (для разработки и тестов).

Безопасность и честность:

* открывается **только** тот файл, который SparkTech сам скопировал в свою
  папку (проверка делается в вызывающем коде), произвольный путь через API
  передать нельзя;
* если системный механизм недоступен или вернул ошибку, возвращается
  понятная причина — интерфейс покажет её, а не «ничего не произошло»;
* процессы запускаются списком аргументов, без ``shell=True``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OpenResult:
    """Результат попытки открыть файл или папку."""

    ok: bool
    message: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message}


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def default_opener_available() -> bool:
    """Есть ли чем открыть файл в системе по умолчанию."""
    if is_windows() or is_macos():
        return True
    return shutil.which("xdg-open") is not None


def _no_window_kwargs() -> dict:
    if not is_windows():
        return {}
    # Проводник и плеер не должны наследовать консоль SparkTech.
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _spawn(argv: list[str]) -> None:
    """Запустить программу и сразу отпустить её (без ожидания закрытия).

    Отдельная функция — чтобы тесты могли подменить запуск и проверить
    ровно ту команду, которая уходит системе.
    """
    subprocess.Popen(argv, close_fds=not is_windows(), **_no_window_kwargs())


def _show_folder(folder: Path) -> OpenResult:
    try:
        if is_windows():
            _spawn(["explorer", str(folder)])
            return OpenResult(True)
        if is_macos():
            _spawn(["open", str(folder)])
            return OpenResult(True)
        opener = shutil.which("xdg-open")
        if not opener:
            return OpenResult(False, "В системе нет xdg-open: откройте папку вручную.")
        _spawn([opener, str(folder)])
        return OpenResult(True)
    except OSError as exc:
        return OpenResult(False, f"Не удалось открыть папку: {exc}")


def open_with_default(path: str | Path) -> OpenResult:
    """Открыть файл программой по умолчанию (плеер, просмотрщик и т. п.)."""
    target = Path(path)
    if not target.exists():
        return OpenResult(False, f"Файл не найден на компьютере: {target}")
    if target.is_dir():
        return _show_folder(target)
    try:
        if is_windows():
            os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606
            return OpenResult(True)
        if is_macos():
            _spawn(["open", str(target)])
            return OpenResult(True)
        opener = shutil.which("xdg-open")
        if not opener:
            return OpenResult(
                False,
                "В системе нет xdg-open: открыть файл автоматически нельзя. "
                "Откройте папку и запустите файл вручную.",
            )
        _spawn([opener, str(target)])
        return OpenResult(True)
    except (OSError, AttributeError) as exc:
        # AttributeError — на не-Windows сборке нет os.startfile.
        return OpenResult(False, f"Не удалось открыть файл: {exc}")


def reveal_in_folder(path: str | Path) -> OpenResult:
    """Показать файл в проводнике (Windows выделит его в папке).

    Если файла уже нет (например, его удалили на телефоне), показываем папку,
    где он лежал, — это полезнее, чем сообщение «не найдено».
    """
    target = Path(path)
    if target.is_dir():
        return _show_folder(target)
    if not target.exists():
        if target.parent.is_dir():
            return _show_folder(target.parent)
        return OpenResult(False, f"Папка не найдена: {target.parent}")
    try:
        if is_windows():
            # explorer возвращает код 1 даже при успехе — код не проверяем,
            # а вот исключение при запуске поймаем.
            _spawn(["explorer", "/select,", str(target)])
            return OpenResult(True)
        if is_macos():
            _spawn(["open", "-R", str(target)])
            return OpenResult(True)
        opener = shutil.which("xdg-open")
        if not opener:
            return OpenResult(False, "В системе нет xdg-open: откройте папку вручную.")
        _spawn([opener, str(target.parent)])
        return OpenResult(True)
    except OSError as exc:
        return OpenResult(False, f"Не удалось открыть проводник: {exc}")


def open_folder(path: str | Path) -> OpenResult:
    """Открыть папку целиком."""
    target = Path(path)
    if not target.exists():
        return OpenResult(False, f"Папка не найдена: {target}")
    return _show_folder(target)


__all__ = [
    "OpenResult",
    "default_opener_available",
    "is_macos",
    "is_windows",
    "open_folder",
    "open_with_default",
    "reveal_in_folder",
]
