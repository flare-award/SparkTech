"""Безопасный запуск внешних команд с тайм-аутом и отменой.

Единственное место в программе, где создаются процессы `adb`. Никаких
`shell=True`, никакой конкатенации строк в командную строку — только
списки аргументов. stderr/stdout декодируются в UTF-8 с заменой, чтобы
кириллица в именах файлов на телефоне не ломала разбор.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Sequence

# Windows: не показывать консольные окна для дочерних процессов.
_CREATE_NO_WINDOW = 0x08000000
_IS_WINDOWS = os.name == "nt"


class CommandError(RuntimeError):
    """Команда завершилась с ошибкой или не смогла запуститься."""


class CommandTimeout(CommandError):
    """Команда не уложилась в отведённый тайм-аут."""


class CommandCancelled(CommandError):
    """Команда была прервана пользователем."""


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """stdout, а если он пуст — stderr (adb часто пишет ошибки туда)."""
        return self.stdout if self.stdout.strip() else self.stderr


def _apply_no_window(kwargs: dict) -> dict:
    if _IS_WINDOWS:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = si
    return kwargs


def run(
    argv: Sequence[str],
    *,
    timeout: float = 30.0,
    stdin_text: str | None = None,
    cancel_event: threading.Event | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Запустить команду и дождаться завершения.

    Бросает CommandCancelled, CommandTimeout или CommandError (если процесс
    вообще не удалось создать). Ненулевой код возврата не является
    исключением — вызывающий код сам решает, как его трактовать.
    """
    argv = [str(a) for a in argv]
    if not argv:
        raise CommandError("Пустая команда")
    if cancel_event is not None and cancel_event.is_set():
        raise CommandCancelled("Операция отменена")

    full_env = dict(os.environ)
    # ADB не должен пытаться качать обновления и не должен зависеть от локали.
    full_env.setdefault("ADB_MDNS", "0")
    if env:
        full_env.update(env)

    kwargs = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=full_env,
    )
    kwargs = _apply_no_window(kwargs)
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except FileNotFoundError as exc:
        raise CommandError(f"Исполняемый файл не найден: {argv[0]}") from exc
    except OSError as exc:  # PermissionError и пр.
        raise CommandError(f"Не удалось запустить {argv[0]}: {exc}") from exc

    deadline = timeout
    try:
        if stdin_text is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        if cancel_event is not None:
            waited = 0.0
            step = 0.1
            while True:
                try:
                    out, err = proc.communicate(timeout=step)
                    break
                except subprocess.TimeoutExpired:
                    waited += step
                    if cancel_event.is_set():
                        _kill(proc)
                        raise CommandCancelled("Операция отменена")
                    if waited >= deadline:
                        _kill(proc)
                        raise CommandTimeout(
                            f"Команда не ответила за {int(deadline)} с: {' '.join(argv[:3])}"
                        )
        else:
            try:
                out, err = proc.communicate(timeout=deadline)
            except subprocess.TimeoutExpired:
                _kill(proc)
                raise CommandTimeout(
                    f"Команда не ответила за {int(deadline)} с: {' '.join(argv[:3])}"
                )
    finally:
        if proc.poll() is None:
            _kill(proc)

    return CommandResult(argv=argv, returncode=proc.returncode, stdout=out or "", stderr=err or "")


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.communicate(timeout=3)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def run_streaming(
    argv: Sequence[str],
    *,
    on_line: Callable[[str], None],
    timeout: float = 60.0,
    idle_timeout: float = 90.0,
    cancel_event: threading.Event | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Запустить команду и отдавать строки stdout по мере появления.

    Нужен для ``find``: так интерфейс показывает реальный прогресс, а
    сканирование можно прервать, не дожидаясь конца команды. Порог
    ``idle_timeout`` защищает от «зависшего» устройства: если adb молчит
    дольше этого времени, команда убивается.
    """
    argv = [str(a) for a in argv]
    if not argv:
        raise CommandError("Пустая команда")
    if cancel_event is not None and cancel_event.is_set():
        raise CommandCancelled("Операция отменена")

    full_env = dict(os.environ)
    full_env.setdefault("ADB_MDNS", "0")
    if env:
        full_env.update(env)

    kwargs = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=full_env,
    )
    kwargs = _apply_no_window(kwargs)
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except FileNotFoundError as exc:
        raise CommandError(f"Исполняемый файл не найден: {argv[0]}") from exc
    except OSError as exc:
        raise CommandError(f"Не удалось запустить {argv[0]}: {exc}") from exc

    lines: "queue.Queue[str]" = queue.Queue()

    def _pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put("")

    def _pump_err() -> None:
        assert proc.stderr is not None
        for _ in proc.stderr:
            pass

    t_out = threading.Thread(target=_pump, daemon=True)
    t_err = threading.Thread(target=_pump_err, daemon=True)
    t_out.start()
    t_err.start()

    collected: list[str] = []
    import time as _time

    deadline = _time.monotonic() + timeout
    last_line = _time.monotonic()
    try:
        while True:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                now = _time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    _kill(proc)
                    raise CommandCancelled("Сканирование отменено")
                if now - last_line > idle_timeout:
                    _kill(proc)
                    raise CommandTimeout(
                        "Устройство перестало отвечать на команду поиска файлов"
                    )
                if now > deadline:
                    _kill(proc)
                    raise CommandTimeout("Команда поиска файлов превысила допустимое время")
                continue
            if line == "":
                break
            last_line = _time.monotonic()
            text = line.rstrip("\r\n")
            collected.append(text)
            on_line(text)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            _kill(proc)
        t_out.join(timeout=1)
        t_err.join(timeout=1)

    return CommandResult(argv=argv, returncode=proc.returncode, stdout="\n".join(collected), stderr="")


def which(name: str) -> str | None:
    return shutil.which(name)


def popen_env_with_bin_dir(bin_dir: str) -> dict[str, str]:
    """Окружение, в котором DLL рядом с adb.exe (AdbWinApi.dll) находятся."""
    path = os.environ.get("PATH", "")
    sep = ";" if _IS_WINDOWS else ":"
    return {"PATH": bin_dir + sep + path} if bin_dir else {}


def is_windows() -> bool:
    return sys.platform.startswith("win")
