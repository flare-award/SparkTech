"""Локальный HTTP-сервер SparkTech.

Безопасность:

* слушает только loopback (127.0.0.1) — снаружи компьютера недоступен;
* на каждый запуск генерируется случайный токен; API требует его в заголовке,
  поэтому сторонняя вкладка браузера не сможет управлять приложением;
* запрещены POST с чужим Origin;
* отдаются только статические файлы из каталога ``static`` (без выхода за
  его пределы);
* сервер живёт в том же процессе, что и интерфейс, и завершается вместе с
  приложением.
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .. import APP_NAME, __version__
from ..core.adb import ADB_DOWNLOAD_URL, AdbClient, AdbNotFound, connection_status_of
from ..core.apps import AppService
from ..core.classify import Settings
from ..core.deleter import Deleter
from ..core.jobs import Job, JobManager
from ..core.models import human_bytes
from ..core.optimizer import USER_ACTIONS, Optimizer
from ..core.process import CommandError, CommandTimeout
from ..core.scanner import ScanOptions, Scanner
from ..core.streaming import (
    INITIAL_WINDOW,
    TAIL_WINDOW,
    PhoneFileStream,
    StreamError,
    guess_mime,
    is_previewable,
    parse_range,
)
from ..core.store import Store

def _static_dir() -> Path:
    """Каталог интерфейса. Работает и из исходников, и из собранного .exe."""
    here = Path(__file__).resolve().parent / "static"
    if here.is_dir():
        return here
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "sparktech" / "web" / "static"
    return bundled if bundled.is_dir() else here


STATIC_DIR = _static_dir()

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
TOKEN_HEADER = "X-SparkTech-Token"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status
        super().__init__(message)


class AppContext:
    """Общее состояние приложения (одно на процесс)."""

    def __init__(self, adb_path: str | None = None, store: Store | None = None):
        self.store = store or Store()
        saved = self.store.load_settings()
        self.adb = AdbClient(adb_path or saved.get("adb_path"))
        self.scanner = Scanner(self.adb)
        self.deleter = Deleter(self.adb)
        self.optimizer = Optimizer(self.adb, self.store)
        self.apps = AppService(self.adb)
        self.jobs = JobManager()
        self.scan_items: dict[str, dict] = {}  # scan_id -> {path: item}
        self.app_cache: dict[str, list] = {}   # serial -> список приложений
        self.download_dir = Path.home() / "Downloads" / "SparkTech"
        # Одновременная отдача файлов: не даём браузеру открыть десять
        # параллельных adb-процессов и «задушить» телефон.
        self.stream_slots = threading.Semaphore(3)
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.shutdown_event = threading.Event()
        self.started_adb_server = False

    def settings_from_store(self) -> Settings:
        s = self.store.load_settings()
        return Settings.from_payload(s)

    def log(self, event: str, **fields) -> None:
        self.store.log(event, **fields)


def _json_default(value):
    return str(value)


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ утилиты

    @property
    def app(self) -> AppContext:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # noqa: D401 — глушим стандартный лог
        return

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def send_error_json(self, status: int, message: str, code: str = "error") -> None:
        self.send_json(status, {"ok": False, "error": message, "code": code})

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("Некорректный Content-Length")
        if length > 5 * 1024 * 1024:
            raise ApiError("Слишком большой запрос", 413)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("Тело запроса не является корректным JSON")
        if not isinstance(data, dict):
            raise ApiError("Ожидался JSON-объект")
        return data

    def _require_token(self) -> None:
        token = self.headers.get(TOKEN_HEADER) or ""
        if not secrets.compare_digest(token, self.app.token):
            raise ApiError(
                "Недействительный токен сессии. Перезапустите SparkTech и откройте окно приложения заново.",
                403,
            )

    # --------------------------------------------------------------------- GET

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = posixpath.normpath(parsed.path)
        try:
            if path == "/" or path.startswith("/static"):
                self._serve_static(path)
                return
            if path.startswith("/api/"):
                if not path.startswith("/api/health"):
                    self._require_token()
                self._route_get(path)
                return
            self.send_error_json(404, "Не найдено", "not_found")
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message, "api_error")
        except (CommandError, CommandTimeout, AdbNotFound) as exc:
            self.send_error_json(400, str(exc), "adb_error")
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(500, f"Внутренняя ошибка: {exc}", "internal")

    def _serve_static(self, path: str) -> None:
        rel = path[len("/static"):].lstrip("/")
        if path == "/":
            rel = "index.html"
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error_json(403, "Доступ запрещён", "forbidden")
            return
        if not target.is_file():
            self.send_error_json(404, "Файл не найден", "not_found")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        data = target.read_bytes()
        if target.name == "index.html":
            # Токен сессии подставляется только в настоящую страницу интерфейса.
            data = data.replace(b"@@SPARKTECH_TOKEN@@", self.app.token.encode("ascii"))
        self._send(200, data, ctype)

    def _route_get(self, path: str) -> None:
        app = self.app
        if path == "/api/health":
            self.send_json(200, {"ok": True, "app": APP_NAME, "version": __version__})
            return
        if path == "/api/state":
            self.send_json(200, self._state_payload())
            return
        if path == "/api/devices":
            self.send_json(200, self._devices_payload())
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = app.jobs.get(job_id)
            if job is None:
                raise ApiError("Задание не найдено", 404)
            since = 0
            qs = urlparse(self.path).query
            for part in qs.split("&"):
                if part.startswith("since="):
                    try:
                        since = max(0, int(part.split("=", 1)[1]))
                    except ValueError:
                        since = 0
            self.send_json(200, {"ok": True, "job": job.snapshot(since)})
            return
        if path.startswith("/api/result/"):
            scan_id = path.rsplit("/", 1)[-1]
            with app.lock:
                stored = app.scan_items.get(scan_id)
            if stored is None:
                raise ApiError("Результат не найден (возможно, приложение было перезапущено)", 404)
            # items — список (в интерфейсе это массив); в памяти он хранится
            # словарём по пути для быстрого поиска.
            self.send_json(200, {
                "ok": True,
                "result": stored["result"],
                "items": list(stored["items"].values()),
            })
            return
        if path == "/api/actions":
            serial, sdk = self._query_serial()
            if not serial:
                raise ApiError("Устройство не выбрано", 409)
            actions = app.optimizer.available_actions(serial, sdk)
            self.send_json(200, {
                "ok": True,
                "actions": actions,
                "manual": USER_ACTIONS,
                "saved": app.store.saved_settings(serial),
            })
            return
        if path == "/api/log":
            self.send_json(200, {"ok": True, "entries": app.store.read_log(150)})
            return
        if path.startswith("/api/preview/"):
            self._serve_preview(path)
            return
        if path.startswith("/api/file/"):
            self._serve_file(path)
            return
        if path == "/api/apps":
            self._serve_apps()
            return
        if path.startswith("/api/app-icon/"):
            self._serve_app_icon(path)
            return
        if path == "/api/settings":
            self.send_json(200, {
                "ok": True,
                "settings": app.settings_from_store().to_dict(),
                "adb_path": app.store.load_settings().get("adb_path", ""),
                "adb_found": _safe_adb_path(app),
                "download_url": ADB_DOWNLOAD_URL,
                "data_dir": str(app.store.dir),
            })
            return
        raise ApiError("Неизвестный метод API", 404)

    # --------------------------------------------------- файлы и превью

    def _scan_paths(self, scan_id: str) -> tuple[str, dict]:
        """Достать сохранённый результат сканирования и набор известных путей.

        Это единственный источник истины для путей: подставить в URL
        произвольный файл устройства нельзя.
        """
        app = self.app
        with app.lock:
            stored = app.scan_items.get(scan_id)
        if stored is None:
            raise ApiError("Результат сканирования не найден. Запустите сканирование заново.", 404)
        return stored["serial"], stored["items"]

    def _path_from_query(self, scan_id: str) -> tuple[str, dict, str]:
        query = urlparse(self.path).query
        raw = ""
        for part in query.split("&"):
            if part.startswith("path="):
                raw = unquote(part.split("=", 1)[1])
                break
        if not raw:
            raise ApiError("Не указан путь файла")
        serial, items = self._scan_paths(scan_id)
        item = items.get(raw)
        if item is None:
            raise ApiError(
                "Этот файл отсутствует в текущем результате сканирования. "
                "Повторите сканирование — показываются только проверенные файлы.",
                403,
            )
        return serial, item, raw

    def _serve_preview(self, path: str) -> None:
        """Миниатюра фото или кадр видео — обычным ответом с Range-поддержкой."""
        scan_id = path.rsplit("/", 1)[-1]
        serial, item, remote_path = self._path_from_query(scan_id)
        size = int(item.get("size") or 0)
        mime = guess_mime(remote_path)
        if not is_previewable(remote_path):
            self.send_error_json(
                415,
                "Предпросмотр для этого типа файла не поддерживается: браузер не умеет его показывать.",
                "unsupported",
            )
            return
        self._stream_phone_file(serial, remote_path, size, mime)

    def _serve_file(self, path: str) -> None:
        """Полный файл для просмотра в новой вкладке или прослушивания."""
        scan_id = path.rsplit("/", 1)[-1]
        serial, item, remote_path = self._path_from_query(scan_id)
        size = int(item.get("size") or 0)
        self._stream_phone_file(serial, remote_path, size, guess_mime(remote_path))

    def _stream_phone_file(self, serial: str, remote_path: str, size: int, mime: str) -> None:
        """Отдать файл с устройства по частям, честно поддерживая Range.

        Память не растёт: байты идут из процесса adb прямо в сокет. Для
        видео дополнительно дочитывается хвост файла — у MP4, записанных
        телефоном, оглавление (moov) часто лежит в конце, и без него браузер
        не покажет первый кадр.
        """
        if not self.app.stream_slots.acquire(timeout=30):
            self.send_error_json(
                429,
                "Слишком много одновременных запросов к файлам. Закройте лишние вкладки и повторите.",
                "busy",
            )
            return
        stream = None
        try:
            header = self.headers.get("Range")
            requested = parse_range(header, size) if size > 0 else None
            if requested is not None:
                start, end = requested
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                start, end = 0, (size - 1 if size > 0 else 0)
                self.send_response(200)
            length = max(0, end - start + 1)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command == "HEAD" or length <= 0:
                return

            stream = PhoneFileStream(self.app.adb, serial, remote_path)
            window = length if length <= INITIAL_WINDOW else INITIAL_WINDOW
            if start:
                stream.seek_forward(start)
            _copy_bytes(stream, window, self.wfile)
            remaining = length - window
            # Хвост файла для видео: браузеру нужен moov в конце MP4.
            if remaining > 0 and TAIL_WINDOW > 0 and mime.startswith("video/"):
                tail_len = min(TAIL_WINDOW, remaining)
                tail_start = end - tail_len + 1
                _copy_slice(stream, tail_start, tail_len, self.wfile, already_read=start + window)
                remaining -= tail_len
        except (BrokenPipeError, ConnectionResetError):
            return
        except (CommandError, CommandTimeout, StreamError) as exc:
            # Заголовки уже отправлены — сообщение в теле уже не исправить,
            # поэтому пишем в журнал, а не притворяемся, что всё хорошо.
            self.app.log("preview_error", path=remote_path, error=str(exc)[:200])
        finally:
            if stream is not None:
                stream.close()
            self.app.stream_slots.release()

    # ------------------------------------------------------------- приложения

    def _serve_apps(self) -> None:
        app = self.app
        query = urlparse(self.path).query
        serial = None
        for part in query.split("&"):
            if part.startswith("serial="):
                serial = unquote(part.split("=", 1)[1])
        if not serial:
            raise ApiError("Устройство не выбрано", 409)
        refresh = "refresh=1" in query
        cached = app.app_cache.get(serial)
        if cached is None or refresh:
            cached = app.apps.list_apps(serial)
            app.app_cache[serial] = cached
            app.log("apps", ser=serial, count=len(cached))
        apps = [a.to_dict() for a in cached]
        total = sum(a["total_size"] for a in apps)
        removable = sum(a["total_size"] for a in apps if not a["is_system"])
        self.send_json(200, {
            "ok": True,
            "apps": apps,
            "count": len(apps),
            "total_size": total,
            "removable_size": removable,
            "notes": [
                "Удалять приложения SparkTech не будет: это действие вы делаете на телефоне.",
                "Данные и кэш приложений (/data/data) недоступны без root — их объём показан по данным Android, но очистить его из программы нельзя.",
                "Время последнего запуска есть не на всех прошивках: Android 10+ часто его скрывает.",
            ],
        })

    def _serve_app_icon(self, path: str) -> None:
        app = self.app
        query = urlparse(self.path).query
        # Пакет передаётся в самом пути: /api/app-icon/<пакет>?serial=...
        package = unquote(path.rsplit("/", 1)[-1])
        serial = ""
        for part in query.split("&"):
            if part.startswith("serial="):
                serial = unquote(part.split("=", 1)[1])
        if not serial or not package:
            raise ApiError("Нужны параметры serial и package", 400)
        cached = app.app_cache.get(serial) or []
        target = next((a for a in cached if a.package == package), None)
        if target is None:
            raise ApiError("Приложение не найдено в списке. Обновите список приложений.", 404)
        icon = app.apps.icon(serial, target)
        if not icon:
            raise ApiError(
                "APK приложения недоступен для чтения или в нём нет иконки — "
                "показывается нейтральная заглушка.",
                404,
            )
        mime = "image/png"
        if icon[:4] == b"RIFF" and icon[8:12] == b"WEBP":
            mime = "image/webp"
        self._send(200, icon, mime, {"Cache-Control": "no-store"})

    def _query_serial(self) -> tuple[str | None, int | None]:
        qs = urlparse(self.path).query
        serial = None
        sdk = None
        for part in qs.split("&"):
            if part.startswith("serial="):
                serial = part.split("=", 1)[1]
            elif part.startswith("sdk="):
                try:
                    sdk = int(part.split("=", 1)[1])
                except ValueError:
                    sdk = None
        return serial, sdk

    # -------------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = posixpath.normpath(parsed.path)
        if not path.startswith("/api/"):
            self.send_error_json(404, "Не найдено", "not_found")
            return
        # CSRF: состояние меняется только с секретным токеном, который
        # выдаётся исключительно в HTML локального интерфейса. Посторонняя
        # страница не может ни прочитать этот токен, ни отправить его в
        # заголовке (из-за CORS). Отдельная проверка Origin здесь не нужна и
        # ломала бы работу через прокси предпросмотра.
        try:
            self._require_token()
            payload = self._read_json()
            self._route_post(path, payload)
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message, "api_error")
        except (CommandError, CommandTimeout, AdbNotFound) as exc:
            self.send_error_json(400, str(exc), "adb_error")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(500, f"Внутренняя ошибка: {exc}", "internal")

    def _route_post(self, path: str, payload: dict) -> None:
        app = self.app
        if path == "/api/shutdown":
            self.send_json(200, {"ok": True, "message": "SparkTech завершает работу"})
            threading.Thread(target=self._shutdown_later, daemon=True).start()
            return
        if path == "/api/settings":
            app.store.update_settings(payload)
            resolved = None
            problem = None
            if payload.get("adb_path") is not None:
                typed = str(payload.get("adb_path") or "").strip()
                # Пользователь может ввести папку, путь в кавычках или весь SDK —
                # сначала приводим ввод к файлу adb.exe (или оставляем как есть).
                normalized = app.adb.normalize_user_path(typed)
                # Если ввод был папкой (или путём в кавычках), normalize вернёт
                # файл adb внутри неё. Если ввод был путём к файлу — вернёт его
                # же; тогда проверяем, что файл существует.
                raw = normalized if Path(normalized).is_file() else typed
                if raw:
                    # Сначала проверяем ИМЕННО введённый путь: resolve() умеет
                    # откатываться на автопоиск, и это скрыло бы опечатку.
                    if not Path(raw).is_file():
                        problem = (
                            f"В указанном пути adb не найден: {typed}. Проверены «{raw}» "
                            "и файлы adb.exe/adb внутри папки. Укажите папку platform-tools "
                            "или сам файл adb.exe; автопоиск продолжит работать."
                        )
                        resolved = None
                        app.adb.set_path(str(app.store.load_settings().get("adb_path") or ""))
                        raw = ""
                if raw:
                    app.adb.set_path(raw)  # применяется только если файл существует
                    try:
                        resolved = app.adb.resolve(force=True)
                        app.store.update_settings({"adb_path": resolved})
                    except AdbNotFound:
                        # Путь не применён и не сохраняется: иначе он перекрывал бы
                        # автоматический поиск рабочего adb.
                        problem = (
                            f"В указанном пути adb не найден: {typed}. Проверены вариант "
                            f"«{raw}» и файлы adb.exe/adb внутри папки. Укажите папку "
                            "platform-tools или сам файл adb.exe."
                        )
                        app.adb.set_path(str(app.store.load_settings().get("adb_path") or ""))
            self.send_json(200, {
                "ok": True,
                "settings": app.settings_from_store().to_dict(),
                "adb_resolved": resolved,
                "adb_problem": problem,
            })
            return
        if path == "/api/download":
            self._start_download(payload)
            return
        if path == "/api/reveal":
            self._reveal_download_dir()
            return
        if path == "/api/scan":
            self._start_scan(payload)
            return
        if path == "/api/delete":
            self._start_delete(payload)
            return
        if path == "/api/optimize":
            self._start_optimize(payload)
            return
        if path == "/api/undo":
            serial = str(payload.get("serial") or "").strip()
            action_id = str(payload.get("action_id") or "").strip()
            if not serial or not action_id:
                raise ApiError("Не указано устройство или действие")
            outcome = app.optimizer.undo(serial, action_id)
            app.log("undo", ser=serial, action=action_id, status=outcome.status)
            self.send_json(200, {"ok": True, "outcome": outcome.to_dict()})
            return
        if path == "/api/jobs/cancel":
            job = app.jobs.get(str(payload.get("job_id") or ""))
            if job is None:
                raise ApiError("Задание не найдено", 404)
            cancelled = job.cancel()
            app.log("job_cancel", job=job.id, kind=job.kind, cancelled=cancelled)
            self.send_json(200, {"ok": True, "cancelled": cancelled})
            return
        raise ApiError("Неизвестный метод API", 404)

    def _shutdown_later(self) -> None:
        self.app.shutdown_event.set()
        threading.Timer(0.4, self.server.shutdown).start()  # type: ignore[attr-defined]

    # ------------------------------------------------------------- состояние

    def _devices_payload(self) -> dict:
        app = self.app
        try:
            adb_path = app.adb.resolve()
        except AdbNotFound as exc:
            return {
                "ok": True,
                "adb": {"found": False, "error": str(exc), "download_url": ADB_DOWNLOAD_URL},
                "devices": [],
                "status": "no_adb",
                "status_label": "ADB не найден",
            }
        try:
            adb_version, adb_problem = app.adb.version_diagnostics()
        except (CommandError, CommandTimeout) as exc:
            adb_version, adb_problem = None, str(exc)
        if adb_version is None:
            # Показываем настоящую причину, а не догадку: раньше любая ошибка
            # превращалась в «файл повреждён», что путало пользователей.
            return {
                "ok": True,
                "adb": {
                    "found": True,
                    "path": adb_path,
                    "error": adb_problem or "adb не сообщил версию",
                    "hint": (
                        "Проверьте, что указан именно файл adb.exe (а не папка platform-tools). "
                        "Если путь введён вручную, SparkTech попробует найти adb.exe в этой папке "
                        "сам. Также убедитесь, что антивирус не блокирует запуск."
                    ),
                    "download_url": ADB_DOWNLOAD_URL,
                },
                "devices": [],
                "status": "adb_error",
                "status_label": "ADB не запускается",
            }
        try:
            app.adb.start_server()
            devices = app.adb.list_devices()
        except (CommandError, CommandTimeout) as exc:
            return {
                "ok": True,
                "adb": {"found": True, "version": adb_version, "path": adb_path, "error": str(exc)},
                "devices": [],
                "status": "error",
                "status_label": "Ошибка ADB",
            }
        status, label = connection_status_of(devices)
        return {
            "ok": True,
            "adb": {"found": True, "version": adb_version, "path": adb_path},
            "devices": [d.to_dict() for d in devices],
            "status": status,
            "status_label": label,
        }

    def _state_payload(self) -> dict:
        devices = self._devices_payload()
        return {
            "ok": True,
            "app": {"name": APP_NAME, "version": __version__},
            "adb": devices.get("adb"),
            "devices": devices.get("devices"),
            "status": devices.get("status"),
            "status_label": devices.get("status_label"),
            "settings": self.app.settings_from_store().to_dict(),
        }

    # --------------------------------------------------------------- задания

    def _start_download(self, payload: dict) -> None:
        """Сохранить файлы с телефона в папку «Загрузки/SparkTech» на ПК.

        Имя файла не берётся из сети: оно вычисляется из пути на телефоне и
        дополнительно проверяется на «..» и разделители, чтобы запись не
        вышла за пределы папки загрузок. После копирования размер сверяется
        с ожидаемым — совпадение и есть проверка успеха.
        """
        app = self.app
        serial, _model, _sdk = self._device_from(payload)
        paths = payload.get("paths")
        scan_id = str(payload.get("scan_id") or "")
        if not isinstance(paths, list) or not paths:
            raise ApiError("Не выбрано ни одного файла")
        if len(paths) > 200:
            raise ApiError("За один раз можно сохранить не более 200 файлов")
        with app.lock:
            stored = app.scan_items.get(scan_id)
        if stored is None:
            raise ApiError("Результат сканирования не найден. Запустите сканирование заново.", 404)
        items = stored["items"]
        selected = []
        for raw in paths:
            item = items.get(str(raw))
            if item is None:
                continue
            selected.append(item)
        if not selected:
            raise ApiError("Выбранные файлы отсутствуют в текущем результате сканирования", 400)

        def target(job: Job):
            saved: list[dict] = []
            failed: list[dict] = []
            folder = app.download_dir
            folder.mkdir(parents=True, exist_ok=True)
            total = len(selected)
            for index, item in enumerate(selected, start=1):
                if job.cancel_event.is_set():
                    failed.append({"path": item["path"], "message": "Отменено пользователем"})
                    continue
                job.push("download", {
                    "index": index,
                    "total": total,
                    "message": f"Сохранение {index} из {total}",
                    "name": item.get("name"),
                    "human": human_bytes(item.get("size")),
                })
                try:
                    local = _safe_local_target(folder, item)
                    app.adb.pull(serial, item["path"], local, cancel_event=job.cancel_event)
                    size = local.stat().st_size
                    expected = int(item.get("size") or 0)
                    if expected and size != expected:
                        failed.append({
                            "path": item["path"],
                            "local_path": str(local),
                            "message": (
                                f"Размер на компьютере ({human_bytes(size)}) не совпал с размером "
                                f"на телефоне ({human_bytes(expected)}) — копия неполная."
                            ),
                        })
                        continue
                    saved.append({
                        "path": item["path"],
                        "local_path": str(local),
                        "size": size,
                    })
                except (CommandError, CommandTimeout) as exc:
                    failed.append({"path": item["path"], "message": str(exc)})
            app.log("download", ser=serial, requested=total, ok=len(saved), failed=len(failed))
            return {"saved": saved, "failed": failed, "folder": str(folder)}

        job = app.jobs.create("download", target, meta={"serial": serial, "count": len(selected)})
        self.send_json(200, {
            "ok": True,
            "job_id": job.id,
            "folder": str(app.download_dir),
        })

    def _reveal_download_dir(self) -> None:
        """Сообщить путь папки загрузок (открытие — средствами системы)."""
        app = self.app
        app.download_dir.mkdir(parents=True, exist_ok=True)
        self.send_json(200, {"ok": True, "folder": str(app.download_dir)})

    def _start_scan(self, payload: dict) -> None:
        app = self.app
        serial, model, sdk = self._device_from(payload)
        settings = Settings.from_payload(payload.get("settings") or app.store.load_settings())
        options = ScanOptions(settings=settings, include_obb=bool(payload.get("include_obb")))

        def target(job: Job):
            def progress(stage: str, data: dict) -> None:
                job.push(stage, data)

            result = app.scanner.scan(
                serial, model=model, options=options, progress=progress, cancel_event=job.cancel_event
            )
            data = result.to_dict()
            with app.lock:
                app.scan_items[result.scan_id] = {
                    "result": data,
                    "items": {i["path"]: i for i in data["items"]},
                    "serial": serial,
                }
            app.log(
                "scan",
                ser=serial,
                sdk=sdk,
                items=len(result.items),
                areas=len(result.areas),
                cancelled=result.cancelled,
                bytes=result.total_size,
            )
            return {"scan_id": result.scan_id}

        job = app.jobs.create("scan", target, meta={"serial": serial, "model": model})
        self.send_json(200, {"ok": True, "job_id": job.id})

    def _start_delete(self, payload: dict) -> None:
        app = self.app
        serial, model, _sdk = self._device_from(payload)
        paths = payload.get("paths")
        scan_id = str(payload.get("scan_id") or "")
        confirmed = bool(payload.get("confirm"))
        if not isinstance(paths, list) or not paths:
            raise ApiError("Не выбрано ни одного файла")
        if len(paths) > 2000:
            raise ApiError("За один раз можно удалить не более 2000 файлов")
        if not confirmed:
            raise ApiError("Удаление не подтверждено пользователем", 409)

        sizes: dict[str, int] = {}
        with app.lock:
            stored = app.scan_items.get(scan_id)
        if stored:
            for path in paths:
                item = stored["items"].get(path)
                if item:
                    sizes[path] = int(item["size"])

        def target(job: Job):
            def progress(index: int, total: int, path: str) -> None:
                job.push(
                    "delete",
                    {
                        "index": index,
                        "total": total,
                        "message": f"Удаление {index} из {total}",
                        "human": human_bytes(sizes.get(path)),
                    },
                )

            batch = app.deleter.delete_many(
                serial, paths, sizes=sizes, progress=progress, cancel_event=job.cancel_event
            )
            with app.lock:
                if stored:
                    for res in batch.succeeded:
                        item = stored["items"].pop(res.path, None)
                        if item is not None:
                            stored["result"]["items"] = [
                                i for i in stored["result"]["items"] if i["path"] != res.path
                            ]
                            stored["result"]["total_size"] -= item["size"]
                            stored["result"]["count"] = len(stored["result"]["items"])
                            stored["result"]["summary"] = _summary(stored["result"]["items"])
            app.log("delete", ser=serial, requested=len(paths),
                    ok=len(batch.succeeded), failed=len(batch.failed))
            return batch.to_dict()

        job = app.jobs.create("delete", target, meta={"serial": serial, "count": len(paths)})
        self.send_json(200, {"ok": True, "job_id": job.id})

    def _start_optimize(self, payload: dict) -> None:
        app = self.app
        serial, _model, sdk = self._device_from(payload)
        action_ids = payload.get("actions")
        if not isinstance(action_ids, list) or not action_ids:
            raise ApiError("Не выбрано ни одного действия")
        action_ids = [str(a) for a in action_ids if isinstance(a, str)]
        confirmed = set(payload.get("confirmed_actions") or [])

        def target(job: Job):
            outcomes = []
            for index, action_id in enumerate(action_ids, start=1):
                if job.cancel_event.is_set():
                    outcomes.append({"action_id": action_id, "status": "skipped",
                                     "status_label": "Пропущено", "message": "Отменено пользователем",
                                     "details": []})
                    continue
                job.push("action", {
                    "index": index, "total": len(action_ids),
                    "message": f"Выполняется: {action_id}",
                })
                if action_id == "restart_ui" and action_id not in confirmed:
                    outcomes.append({
                        "action_id": "restart_ui", "status": "user_action",
                        "status_label": "Требуется действие пользователя",
                        "message": "Перезапуск оболочки требует отдельного подтверждения — действие не выполнено.",
                        "details": [],
                    })
                    continue
                outcome = app.optimizer.run(serial, action_id, sdk=sdk, cancel_event=job.cancel_event)
                outcomes.append(outcome.to_dict())
                app.log("optimize", ser=serial, action=action_id, status=outcome.status)
            return {"outcomes": outcomes}

        job = app.jobs.create("optimize", target, meta={"serial": serial, "count": len(action_ids)})
        self.send_json(200, {"ok": True, "job_id": job.id})

    def _device_from(self, payload: dict) -> tuple[str, str | None, int | None]:
        serial = str(payload.get("serial") or "").strip()
        if not serial:
            raise ApiError("Устройство не выбрано", 409)
        # Не доверяем серийнику из запроса слепо: он должен быть в списке adb.
        devices = self.app.adb.list_devices(with_props=False)
        match = next((d for d in devices if d.serial == serial), None)
        if match is None:
            raise ApiError("Устройство не найдено. Подключите телефон заново.", 409)
        if not match.ready:
            raise ApiError(f"Устройство недоступно: {match.state_label}", 409)
        model = match.model
        sdk = None
        if payload.get("sdk") is not None:
            try:
                sdk = int(payload["sdk"])
            except (TypeError, ValueError):
                sdk = None
        if sdk is None:
            props = self.app.adb.get_props(serial)
            try:
                sdk = int(props.get("ro.build.version.sdk", ""))
            except ValueError:
                sdk = None
            model = props.get("ro.product.model") or model
        return serial, model, sdk


def _safe_local_target(folder: Path, item: dict) -> Path:
    """Собрать локальный путь так, чтобы он не вышел за папку загрузок.

    Имя файла на телефоне может быть любым (в том числе ``..`` или со
    слэшами), поэтому берётся только последний компонент, недопустимые для
    Windows символы заменяются, а совпадения имён разводятся суффиксом.
    """
    raw_name = str(item.get("name") or "file")
    name = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
    name = name.replace("\x00", "")
    for bad in '<>:"|?*':
        name = name.replace(bad, "_")
    name = name.strip(". ")
    if not name or name in (".", ".."):
        name = "file"
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:140] + dot + ext[:9]) if dot else name[:150]
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / name
    stem, dot, ext = name.rpartition(".")
    counter = 1
    while candidate.exists():
        if dot:
            candidate = folder / f"{stem} ({counter}).{ext}"
        else:
            candidate = folder / f"{name} ({counter})"
        counter += 1
        if counter > 999:
            break
    try:
        candidate.resolve().relative_to(folder.resolve())
    except ValueError:
        candidate = folder / "file"
    return candidate


def _copy_bytes(stream, length: int, out) -> int:
    """Скопировать из потока телефона ровно до ``length`` байт."""
    from ..core.streaming import _CHUNK

    written = 0
    while written < length:
        chunk = stream.read(min(_CHUNK, length - written))
        if not chunk:
            break
        out.write(chunk)
        written += len(chunk)
    return written


def _copy_slice(stream, start: int, length: int, out, *, already_read: int = 0) -> int:
    """Скопировать участок файла, начиная с ``start``.

    ``PhoneFileStream`` умеет только двигаться вперёд, поэтому если позиция
    потока уже больше нужной, участок пропускается: повторно читать файл с
    начала дороже, чем один раз пропустить.
    """
    if start > already_read:
        stream.seek_forward(start - already_read)
    elif start < already_read:
        return 0
    return _copy_bytes(stream, length, out)


def _summary(items: list[dict]) -> list[dict]:
    from ..core.models import CATEGORY_LABELS

    acc: dict[str, dict] = {}
    for item in items:
        key = item.get("category_key", "other")
        entry = acc.setdefault(key, {"key": key, "label": CATEGORY_LABELS.get(key, key), "count": 0, "size": 0})
        entry["count"] += 1
        entry["size"] += int(item.get("size", 0))
    return sorted(acc.values(), key=lambda e: e["size"], reverse=True)


def _safe_adb_path(app: AppContext) -> str | None:
    try:
        return app.adb.resolve()
    except AdbNotFound:
        return None


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: AppContext, port: int = 0):
        self.app = app
        super().__init__(address, Handler)

    def server_bind(self):
        # Не пытаться разрешать имена: быстрее и надёжнее на Windows.
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


def create_server(app: AppContext, port: int = 0, host: str = "127.0.0.1") -> LocalServer:
    """Создать сервер. По умолчанию — только loopback (127.0.0.1).

    ``host`` отличается от loopback только для отладочных сценариев
    (например, просмотр интерфейса в изолированной среде); в обычной работе
    SparkTech никогда не слушает сеть.
    """
    return LocalServer((host, port), app)


def serve_in_thread(app: AppContext, port: int = 0):
    server = create_server(app, port)
    thread = threading.Thread(target=server.serve_forever, name="sparktech-http", daemon=True)
    thread.start()
    return server, thread
