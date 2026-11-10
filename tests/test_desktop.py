"""Тесты открытия файлов и папок средствами операционной системы.

Главное, что здесь проверяется: SparkTech не запускает ничего через shell
(никаких `shell=True` с чужим путём), правильно выбирает команду под каждую
ОС и честно сообщает об ошибке, если открыть нечем.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sparktech.core import desktop


class FakeRun:
    """Подмена запуска процесса: запоминает команды и может «падать»."""

    def __init__(self, code: int = 0, error: Exception | None = None):
        self.calls: list[list[str]] = []
        self.code = code
        self.error = error

    def __call__(self, argv, **kwargs):
        assert kwargs.get("shell") in (None, False), "shell не используется"
        if self.error is not None:
            raise self.error
        self.calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, self.code, "", "")


def test_open_with_default_windows(monkeypatch, tmp_path):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x")
    started: list[str] = []
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "is_macos", lambda: False)
    monkeypatch.setattr(desktop.os, "startfile", lambda p: started.append(p), raising=False)

    result = desktop.open_with_default(target)

    assert result.ok is True
    assert started == [str(target)]


def test_open_with_default_macos(monkeypatch, tmp_path):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x")
    run = FakeRun()
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "is_macos", lambda: True)
    monkeypatch.setattr(desktop, "_spawn", run)

    assert desktop.open_with_default(target).ok is True
    assert run.calls == [["open", str(target)]]


def test_open_with_default_linux_uses_xdg_open(monkeypatch, tmp_path):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x")
    run = FakeRun()
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "is_macos", lambda: False)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(desktop, "_spawn", run)

    assert desktop.open_with_default(target).ok is True
    assert run.calls == [["/usr/bin/xdg-open", str(target)]]


def test_open_with_default_without_xdg_open(monkeypatch, tmp_path):
    """На Linux без xdg-open автозапуска нет — об этом говорим прямо."""
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x")
    run = FakeRun()
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "is_macos", lambda: False)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    monkeypatch.setattr(desktop, "_spawn", run)

    result = desktop.open_with_default(target)

    assert result.ok is False
    assert "xdg-open" in result.message
    assert run.calls == []


def test_open_with_default_missing_file(monkeypatch, tmp_path):
    run = FakeRun()
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "_spawn", run)

    result = desktop.open_with_default(tmp_path / "нет.mp4")

    assert result.ok is False
    assert run.calls == []


def test_open_with_default_catches_os_error(monkeypatch, tmp_path):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x")
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "is_macos", lambda: True)

    run = FakeRun(error=OSError("нет программы"))
    monkeypatch.setattr(desktop, "_spawn", run)

    result = desktop.open_with_default(target)
    assert result.ok is False
    assert "нет программы" in result.message


def test_reveal_in_folder_windows(monkeypatch, tmp_path):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x")
    run = FakeRun(code=1)  # explorer возвращает 1 даже при успехе
    monkeypatch.setattr(desktop, "is_windows", lambda: True)
    monkeypatch.setattr(desktop, "is_macos", lambda: False)
    monkeypatch.setattr(desktop, "_spawn", run)

    result = desktop.reveal_in_folder(target)

    assert result.ok is True, "код возврата explorer не считается ошибкой"
    assert run.calls == [["explorer", "/select,", str(target)]]


def test_reveal_in_folder_macos(monkeypatch, tmp_path):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x")
    run = FakeRun()
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "is_macos", lambda: True)
    monkeypatch.setattr(desktop, "_spawn", run)

    assert desktop.reveal_in_folder(target).ok is True
    assert run.calls == [["open", "-R", str(target)]]


def test_reveal_in_folder_linux_opens_parent(monkeypatch, tmp_path):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x")
    run = FakeRun()
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "is_macos", lambda: False)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(desktop, "_spawn", run)

    assert desktop.reveal_in_folder(target).ok is True
    assert run.calls == [["/usr/bin/xdg-open", str(tmp_path)]]


def test_open_folder_requires_existing_directory(monkeypatch, tmp_path):
    run = FakeRun()
    monkeypatch.setattr(desktop, "_spawn", run)

    result = desktop.open_folder(tmp_path / "нет-такой")

    assert result.ok is False
    assert run.calls == []


def test_open_folder_creates_nothing_and_opens(monkeypatch, tmp_path):
    run = FakeRun()
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "is_macos", lambda: True)
    monkeypatch.setattr(desktop, "_spawn", run)

    assert desktop.open_folder(tmp_path).ok is True
    assert run.calls == [["open", str(tmp_path)]]


def test_default_opener_available(monkeypatch):
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "is_macos", lambda: False)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert desktop.default_opener_available() is False

    monkeypatch.setattr(desktop, "is_macos", lambda: True)
    assert desktop.default_opener_available() is True


def test_result_serialises_for_api():
    assert desktop.OpenResult(True).to_dict()["ok"] is True
    assert desktop.OpenResult(True, "").to_dict() == {"ok": True, "message": ""}

    bad = desktop.OpenResult(False, "не открылось").to_dict()
    assert bad == {"ok": False, "message": "не открылось"}


def test_open_uses_no_shell_and_survives_quotes(monkeypatch, tmp_path):
    """Имя файла с пробелами и кавычками не должно попадать в shell."""
    target = tmp_path / 'clip "с кавычками" & rm -rf.txt'
    target.write_bytes(b"x")
    run = FakeRun()
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "is_macos", lambda: False)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(desktop, "_spawn", run)

    assert desktop.open_with_default(target).ok is True
    assert run.calls == [["/usr/bin/xdg-open", str(target)]]


def test_open_with_default_windows_startfile_missing(monkeypatch, tmp_path):
    """На старых сборках startfile может отсутствовать — нужен понятный ответ."""
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x")
    monkeypatch.setattr(desktop, "is_windows", lambda: True)

    def no_startfile(*_a, **_k):
        raise AttributeError("startfile")

    monkeypatch.setattr(desktop.os, "startfile", no_startfile, raising=False)

    result = desktop.open_with_default(target)
    assert result.ok is False
    assert "startfile" in result.message or "открыть" in result.message


@pytest.mark.parametrize("name", ["video.mp4", "фото 2024.png"])
def test_reveal_missing_file_falls_back_to_parent(monkeypatch, tmp_path, name):
    """Если файла уже нет, показываем хотя бы папку, где он должен быть."""
    run = FakeRun()
    monkeypatch.setattr(desktop, "is_windows", lambda: False)
    monkeypatch.setattr(desktop, "is_macos", lambda: True)
    monkeypatch.setattr(desktop, "_spawn", run)

    result = desktop.reveal_in_folder(Path(tmp_path) / name)

    assert result.ok is True
    assert run.calls == [["open", str(tmp_path)]]
