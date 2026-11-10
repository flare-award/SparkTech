"""Безопасное удаление файлов на устройстве.

Гарантии:

* каждый путь проверяется :func:`is_safe_deletable` — системные каталоги,
  данные приложений и корни томов удалить нельзя;
* путь передаётся в оболочку только через :func:`quote_remote_path`;
* перед удалением файл проверяется (существует, обычный файл, не
  симлинк) и сравнивается размер: если файл изменился после сканирования,
  он не удаляется;
* удаляется по одному файлу, каждый результат фиксируется отдельно;
* ничего не удаляется автоматически — только по явному списку от
  пользователя, после подтверждения в интерфейсе.
"""

from __future__ import annotations

import threading
from typing import Iterable

from .adb import AdbClient
from .models import DeleteResult, RemoteFile
from .process import CommandCancelled, CommandError, CommandTimeout
from .remote_path import is_safe_deletable, quote_remote_path


class DeleteBatch:
    """Результат пакетного удаления."""

    def __init__(self) -> None:
        self.results: list[DeleteResult] = []

    @property
    def succeeded(self) -> list[DeleteResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[DeleteResult]:
        return [r for r in self.results if not r.ok]

    @property
    def freed_bytes(self) -> int:
        return sum(getattr(r, "size", 0) or 0 for r in self.succeeded)

    def to_dict(self) -> dict:
        return {
            "deleted": [r.to_dict() for r in self.succeeded],
            "failed": [r.to_dict() for r in self.failed],
            "deleted_count": len(self.succeeded),
            "failed_count": len(self.failed),
            "freed_bytes": self.freed_bytes,
            "cancelled": getattr(self, "cancelled", False),
        }


def validate_batch(paths: Iterable[str]) -> tuple[list[str], list[DeleteResult]]:
    """Отфильтровать недопустимые пути до обращения к устройству."""
    allowed: list[str] = []
    rejected: list[DeleteResult] = []
    seen: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str):
            rejected.append(DeleteResult(path=str(raw), ok=False, message="Путь не является строкой"))
            continue
        if raw in seen:
            continue
        seen.add(raw)
        ok, reason = is_safe_deletable(raw)
        if not ok:
            rejected.append(DeleteResult(path=raw, ok=False, message=reason))
        elif raw.endswith("/*") or ".." in raw:
            rejected.append(
                DeleteResult(path=raw, ok=False, message="Шаблоны и '..' в путях запрещены")
            )
        else:
            allowed.append(raw)
    return allowed, rejected


class Deleter:
    def __init__(self, adb: AdbClient):
        self.adb = adb

    def _verify(self, serial: str, path: str) -> tuple[bool, str]:
        """Проверить, что объект — обычный файл и путь указывает туда же."""
        quoted = quote_remote_path(path)
        cmd = (
            f"if [ -L {quoted} ]; then echo SYMLINK; "
            f"elif [ -f {quoted} ]; then stat -c '%s' {quoted} 2>/dev/null || echo FILE; "
            f"elif [ -d {quoted} ]; then echo DIR; "
            f"else echo MISSING; fi"
        )
        try:
            res = self.adb.shell(serial, cmd, timeout=30)
        except (CommandError, CommandTimeout) as exc:
            return False, f"Не удалось проверить файл: {exc}"
        out = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
        if out == "SYMLINK":
            return False, "Это символическая ссылка — удаление через SparkTech не выполняется"
        if out == "DIR":
            return False, "Это каталог, а не файл — такой объект не удаляется"
        if out == "MISSING":
            return False, "Файл уже отсутствует на устройстве"
        return True, out

    def delete_many(
        self,
        serial: str,
        paths: Iterable[str],
        *,
        sizes: dict[str, int] | None = None,
        verify_size: bool = True,
        progress=None,
        cancel_event: threading.Event | None = None,
    ) -> DeleteBatch:
        sizes = sizes or {}
        batch = DeleteBatch()
        allowed, rejected = validate_batch(paths)
        batch.results.extend(rejected)

        for index, path in enumerate(allowed, start=1):
            if cancel_event is not None and cancel_event.is_set():
                batch.cancelled = True  # type: ignore[attr-defined]
                break
            if progress:
                progress(index, len(allowed), path)
            batch.results.append(self.delete_one(serial, path, expected_size=sizes.get(path), verify_size=verify_size))
        return batch

    def delete_one(
        self,
        serial: str,
        path: str,
        *,
        expected_size: int | None = None,
        verify_size: bool = True,
    ) -> DeleteResult:
        ok, reason = is_safe_deletable(path)
        if not ok:
            return DeleteResult(path=path, ok=False, message=reason)

        verified_size = 0
        if verify_size:
            exists, info = self._verify(serial, path)
            if not exists:
                return DeleteResult(path=path, ok=False, message=info)
            verified_size = int(info) if info.isdigit() else 0
            if expected_size is not None and info.isdigit() and int(info) != expected_size:
                return DeleteResult(
                    path=path,
                    ok=False,
                    message=(
                        f"Размер файла на устройстве ({int(info)} Б) отличается от размера при "
                        f"сканировании ({expected_size} Б). Файл мог измениться — удаление отменено."
                    ),
                )

        quoted = quote_remote_path(path)
        try:
            res = self.adb.shell(serial, f"rm -f -- {quoted}", timeout=45)
        except CommandCancelled:
            return DeleteResult(path=path, ok=False, message="Операция отменена")
        except CommandTimeout:
            return DeleteResult(path=path, ok=False, message="Устройство не ответило на команду удаления")
        except CommandError as exc:
            return DeleteResult(path=path, ok=False, message=f"Ошибка adb: {exc}")

        stderr = (res.stderr or "").strip()
        stdout = (res.stdout or "").strip()
        # Android-rm часто возвращает 0 даже при ошибке прав, поэтому
        # обязательна проверка результата.
        still_there = _verify_still_exists(self, serial, path)
        if still_there:
            detail = stderr or stdout or "Файл остался на месте"
            return DeleteResult(
                path=path,
                ok=False,
                message=f"Удаление не выполнено: {detail}",
            )
        return DeleteResult(
            path=path,
            ok=True,
            message="Удалено",
            size=verified_size or (expected_size or 0),
        )


def _verify_still_exists(deleter: Deleter, serial: str, path: str) -> bool:
    """Существует ли ещё файл после rm (учитываем отсутствие прав)."""
    quoted = quote_remote_path(path)
    try:
        res = deleter.adb.shell(serial, f"[ -e {quoted} ] && echo YES || echo NO", timeout=25)
    except (CommandError, CommandTimeout):
        # Не смогли проверить — не выдаём ложный успех.
        return True
    return res.stdout.strip().endswith("YES")
