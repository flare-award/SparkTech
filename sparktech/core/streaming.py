"""Потоковая выдача файлов с телефона в интерфейс (превью и просмотр).

Идея простая и честная: не изобретать свой декодер картинок и видео, а
отдать браузеру настоящие байты файла с устройства. Браузер умеет рисовать
JPEG/PNG/WebP и **первый кадр MP4**, если сервер поддерживает HTTP Range —
именно так видео начинает играть до полной загрузки.

Что важно для безопасности и памяти:

* байты **не** накапливаются в памяти целиком: ``adb exec-out cat``
  запускается один раз, а ответ уходит в сокет кусками;
* для MP4 последний кусок (moov может лежать в конце файла) дочитывается
  отдельным коротким запросом — иначе браузер не покажет кадр у видео,
  записанных телефоном;
* отдаются только файлы, которые есть в текущем результате сканирования:
  подставить произвольный путь в URL нельзя;
* в интерфейс уходит ``no-store``: файлы не кэшируются на диск браузера.

Ограничение, о котором нужно говорить пользователю: если браузер/WebView2
не поддерживает кодек (например, HEVC на некоторых сборках), кадр не
появится. Это ограничение браузера, а не «повреждение файла».
"""

from __future__ import annotations

import mimetypes
import os
import subprocess
import threading
from typing import BinaryIO

from .process import CommandError, CommandTimeout, _apply_no_window

_CHUNK = 256 * 1024

# Сколько байт из начала файла отдавать, чтобы браузер успел показать кадр.
INITIAL_WINDOW = 4 * 1024 * 1024
# Максимальный размер одного «прыжка» в конец файла за moov.
TAIL_WINDOW = 2 * 1024 * 1024


class StreamError(RuntimeError):
    """Не удалось начать чтение файла с устройства."""


def guess_mime(path: str) -> str:
    """MIME по расширению (без обращения к системе и без сети)."""
    ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path.rsplit("/", 1)[-1] else ""
    known = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
        ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif",
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
        ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".3gp": "video/3gpp",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
        ".wav": "audio/wav", ".flac": "audio/flac",
        ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
    }
    if ext in known:
        return known[ext]
    guessed, _ = mimetypes.guess_type("file" + ext)
    return guessed or "application/octet-stream"


def is_previewable(path: str) -> bool:
    return guess_mime(path).split(";")[0].split("/")[0] in ("image", "video")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Разобрать ``Range: bytes=...`` в пару (start, end) включительно.

    Поддерживаются три формы: ``bytes=a-b``, ``bytes=a-`` и ``bytes=-n``.
    Некорректные и невыполнимые диапазоны возвращают None — тогда сервер
    отвечает 200 с началом файла (это допустимо по RFC 9110 и не ломает
    воспроизведение: браузер просто запросит нужное ещё раз).
    """
    if not header or not header.lower().startswith("bytes="):
        return None
    if size <= 0:
        return None
    spec = header.split("=", 1)[1].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_raw, _, end_raw = spec.partition("-")
    start_raw, end_raw = start_raw.strip(), end_raw.strip()
    try:
        if start_raw == "":
            if not end_raw:
                return None
            length = int(end_raw)
            if length <= 0:
                return None
            start = max(0, size - length)
            return start, size - 1
        start = int(start_raw)
        if start < 0 or start >= size:
            return None
        end = size - 1 if not end_raw else min(int(end_raw), size - 1)
        if end < start:
            return None
        return start, end
    except ValueError:
        return None


class _AdbReader:
    """Один процесс ``adb exec-out cat`` с возможностью «прыгнуть» по файлу."""

    def __init__(self, argv: list[str], env: dict[str, str] | None = None):
        self.argv = argv
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.skip = 0
        self._lock = threading.Lock()

    def _spawn(self) -> subprocess.Popen:
        full_env = dict(os.environ)
        full_env.setdefault("ADB_MDNS", "0")
        if self.env:
            full_env.update(self.env)
        kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=full_env,
        )
        kwargs = _apply_no_window(kwargs)
        try:
            return subprocess.Popen(self.argv, **kwargs)
        except OSError as exc:
            raise StreamError(f"Не удалось запустить adb: {exc}") from exc

    def _close(self) -> None:
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
            except OSError:
                pass
            try:
                if self.proc.stdout is not None:
                    self.proc.stdout.close()
            except OSError:
                pass
            self.proc = None

    def read(self, count: int) -> bytes:
        """Прочитать до ``count`` байт (может вернуть меньше в конце файла)."""
        if count <= 0:
            return b""
        with self._lock:
            out = bytearray()
            guard = 0
            while len(out) < count and guard < 8:
                guard += 1
                if self.skip:
                    if not self._discard(self.skip):
                        break
                    continue
                if self.proc is None:
                    self.proc = self._spawn()
                assert self.proc.stdout is not None
                chunk = self.proc.stdout.read(min(_CHUNK, count - len(out)))
                if not chunk:
                    break
                out.extend(chunk)
            return bytes(out)

    def _discard(self, amount: int) -> bool:
        """Пропустить вперёд по файлу (закрыть процесс и запросить новый)."""
        # read() уже держит блокировку, поэтому здесь без повторного захвата.
        self._close()
        self.proc = self._spawn()
        assert self.proc.stdout is not None
        left = amount
        while left > 0:
            chunk = self.proc.stdout.read(min(_CHUNK, left))
            if not chunk:
                self.skip = 0
                return False
            left -= len(chunk)
        self.skip = 0
        return True

    def seek_forward(self, amount: int) -> None:
        self.skip += max(0, amount)

    def close(self) -> None:
        with self._lock:
            self._close()


class PhoneFileStream:
    """Чтение файла с телефона в стиле файла: ``read(n)`` и ``seek_forward``."""

    def __init__(self, adb, serial: str, path: str):
        from .remote_path import quote_remote_path

        self.adb = adb
        self.serial = serial
        self.path = path
        self._quoted = quote_remote_path(path)
        self._reader = _AdbReader(
            adb._argv(["-s", serial, "exec-out", f"cat {self._quoted}"]),  # noqa: SLF001
            env=adb._env(),  # noqa: SLF001
        )
        self._closed = False

    def read(self, count: int) -> bytes:
        if self._closed:
            return b""
        return self._reader.read(count)

    def seek_forward(self, amount: int) -> None:
        if amount > 0:
            self._reader.seek_forward(amount)

    def close(self) -> None:
        self._closed = True
        self._reader.close()

    def __enter__(self) -> "PhoneFileStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def stream_slice(
    stream: PhoneFileStream,
    start: int,
    length: int,
    on_chunk,
) -> None:
    """Прочитать ровно ``length`` байт начиная с ``start`` и отдать кусками."""
    if start:
        stream.seek_forward(start)
    left = length
    while left > 0:
        chunk = stream.read(min(_CHUNK, left))
        if not chunk:
            return
        left -= len(chunk)
        on_chunk(chunk)


def read_head(stream: BinaryIO, limit: int) -> bytes:
    """Вспомогательное: прочитать не больше ``limit`` байт (для тестов)."""
    return stream.read(limit) or b""


__all__ = [
    "INITIAL_WINDOW",
    "TAIL_WINDOW",
    "PhoneFileStream",
    "StreamError",
    "CommandError",
    "CommandTimeout",
    "guess_mime",
    "is_previewable",
    "parse_range",
    "read_head",
    "stream_slice",
]
