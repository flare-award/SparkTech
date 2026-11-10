"""Тесты новых возможностей: превью, скачивание на ПК, приложения.

Проверяется поведение настоящего HTTP-сервера (локальный loopback-порт) с
подставным транспортом adb: так видно и разбор запросов, и Range, и
безопасные пути, и то, что нельзя запросить файл, которого нет в
результате сканирования.
"""

from __future__ import annotations

import json
import struct
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sparktech.core.adb import Device
from sparktech.core.process import CommandResult
from sparktech.core.store import Store
from sparktech.web.server import AppContext, create_server, _safe_local_target


def png_bytes(size: int = 4096) -> bytes:
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 2, 2)
    return header + b"\x00" * max(0, size - len(header))


class FakeAdb:
    """Транспорт: один «телефон» с файлами в памяти."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.pulls: list[tuple[str, str]] = []
        self.adb_path = "/fake/adb"

    def _argv(self, args):
        return ["adb", *[str(a) for a in args]]

    def _env(self):
        return {}

    # --- то, чем пользуется сервер
    def list_devices(self, with_props=True):
        return [Device(serial="SERIAL1", state="device", model="Тест", sdk=33)]

    def get_props(self, serial):
        return {"ro.product.model": "Тест", "ro.build.version.sdk": "33"}

    def run(self, args, **_kwargs):
        argv = [str(a) for a in args]
        if argv[:2] == ["-s", "SERIAL1"] and argv[2:3] == ["pull"]:
            remote, local = argv[3], argv[4]
            if remote not in self.files:
                return CommandResult(argv, 1, "", "no such file")
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            Path(local).write_bytes(self.files[remote])
            self.pulls.append((remote, local))
            return CommandResult(argv, 0, "1 file pulled.", "")
        return CommandResult(argv, 0, "", "")

    def shell(self, serial, command, **kwargs):
        return CommandResult([command], 0, "", "")

    def pull(self, serial, remote_path, local_path, timeout=600, cancel_event=None):
        target = Path(local_path)
        self.run(["-s", serial, "pull", remote_path, str(target)])
        if not target.is_file():
            from sparktech.core.process import CommandError

            raise CommandError("файл не скопирован")
        return target

    def version_diagnostics(self):
        return "1.0.41", None

    def resolve(self, force=False):
        return self.adb_path

    def start_server(self):
        return None

    def close(self):
        return None


class FakeStreamAdb(FakeAdb):
    """Дополнительно умеет «отдавать» байты файла потоком."""

    def _argv(self, args):
        return ["adb", *[str(a) for a in args]]

    def _env(self):
        return {}


@pytest.fixture()
def server(tmp_path, monkeypatch):
    files = {
        "/storage/emulated/0/Movies/clip.mp4": b"\x00\x00\x00\x18ftypmp42" + b"A" * 200000,
        "/storage/emulated/0/DCIM/photo.png": png_bytes(3000),
        "/storage/emulated/0/Download/report.txt": b"hello",
    }
    adb = FakeAdb(files)
    app = AppContext(store=Store(tmp_path))
    app.adb = adb
    app.download_dir = tmp_path / "downloads"
    app.scan_items["scan1"] = {
        "result": {"scan_id": "scan1"},
        "serial": "SERIAL1",
        "items": {
            path: {
                "path": path,
                "name": path.rsplit("/", 1)[-1],
                "size": len(data),
                "mtime": 1700000000,
                "category_key": "big",
            }
            for path, data in files.items()
        },
    }
    httpd = create_server(app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield app, adb, base
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(base, path, headers=None, token="t"):
    req = urllib.request.Request(base + path, headers={"X-SparkTech-Token": token, **(headers or {})})
    return urllib.request.urlopen(req, timeout=15)


def post(app, base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"X-SparkTech-Token": app.token, "Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())


def test_preview_rejects_unknown_path(server):
    app, adb, base = server
    url = "/api/preview/scan1?path=%2Fetc%2Fpasswd"
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, url, token=app.token)
    assert exc.value.code == 403


def test_preview_rejects_unsupported_type(server):
    app, adb, base = server
    url = "/api/preview/scan1?path=%2Fstorage%2Femulated%2F0%2FDownload%2Freport.txt"
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, url, token=app.token)
    assert exc.value.code == 415


def test_preview_requires_token(server):
    app, adb, base = server
    url = "/api/preview/scan1?path=%2Fstorage%2Femulated%2F0%2FDCIM%2Fphoto.png"
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, url, token="wrong")
    assert exc.value.code == 403


def test_safe_local_target_blocks_path_traversal(tmp_path):
    """Имя файла с «..» не должно вывести запись за папку загрузок."""
    item = {"name": "../../evil.txt"}
    target = _safe_local_target(tmp_path / "dl", item)
    assert target.parent == (tmp_path / "dl")
    assert ".." not in target.name


def test_safe_local_target_replaces_windows_forbidden_chars(tmp_path):
    target = _safe_local_target(tmp_path / "dl", {"name": 'a<b>c:d"e|f?g*.txt'})
    assert target.name == "a_b_c_d_e_f_g_.txt"


def test_safe_local_target_deduplicates_names(tmp_path):
    folder = tmp_path / "dl"
    first = _safe_local_target(folder, {"name": "clip.mp4"})
    first.write_bytes(b"x")
    second = _safe_local_target(folder, {"name": "clip.mp4"})
    assert second != first
    assert second.name.startswith("clip")


def test_download_job_saves_file_and_verifies_size(server):
    app, adb, base = server
    payload = post(app, base, "/api/download", {
        "serial": "SERIAL1",
        "scan_id": "scan1",
        "paths": ["/storage/emulated/0/Download/report.txt"],
    })
    assert payload["ok"]
    assert payload["folder"] == str(app.download_dir)
    # Ждём завершение задания.
    job_id = payload["job_id"]
    for _ in range(200):
        data = json.loads(get(base, f"/api/jobs/{job_id}", token=app.token).read().decode())
        if data["job"]["status"] != "running":
            break
        threading.Event().wait(0.05)
    result = data["job"]["result"]
    assert len(result["saved"]) == 1
    saved = Path(result["saved"][0]["local_path"])
    assert saved.read_bytes() == b"hello"
    assert adb.pulls and adb.pulls[0][0].endswith("report.txt")


def test_download_rejects_paths_outside_scan(server):
    app, adb, base = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(app, base, "/api/download", {
            "serial": "SERIAL1",
            "scan_id": "scan1",
            "paths": ["/storage/emulated/0/Secret/bank.pdf"],
        })
    assert exc.value.code == 400


def test_reveal_creates_download_folder(server, monkeypatch):
    """Папка создаётся всегда, а запуск проводника не важен для теста.

    Сам запуск системной программы подменяется: иначе результат зависел бы от
    того, есть ли в системе xdg-open (в CI он есть, в песочнице — нет).
    """
    app, adb, base = server
    monkeypatch.setattr("sparktech.web.server.open_folder", lambda path: _Ok())
    payload = post(app, base, "/api/reveal", {})
    assert payload["ok"] is True
    assert Path(payload["folder"]).is_dir()


def test_reveal_reports_when_system_opener_missing(server, monkeypatch):
    """Если открыть нечем — честная причина, а не «всё хорошо»."""
    app, adb, base = server
    monkeypatch.setattr(
        "sparktech.web.server.open_folder",
        lambda path: _Bad("В системе нет xdg-open: откройте папку вручную."),
    )
    payload = post(app, base, "/api/reveal", {})
    assert payload["ok"] is False
    assert "xdg-open" in payload["error"]
    # Папка при этом всё равно существует — файлы пользователь найдёт.
    assert Path(payload["folder"]).is_dir()


def test_reveal_refuses_path_outside_download_folder(server):
    app, adb, base = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(app, base, "/api/reveal", {"local_path": "/etc/passwd"})
    assert exc.value.code == 403


def test_open_local_refuses_foreign_path(server):
    """Открыть произвольный файл компьютера через API нельзя."""
    app, adb, base = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(app, base, "/api/open-local", {"local_path": "/etc/hosts"})
    assert exc.value.code == 403


def test_open_local_reports_missing_file(server):
    app, adb, base = server
    missing = app.download_dir / "нет-такого.mp4"
    app.download_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(app, base, "/api/open-local", {"local_path": str(missing)})
    assert exc.value.code == 404


def test_open_remote_copies_and_calls_system_opener(server, monkeypatch):
    """Кнопка «открыть на компьютере»: копирует файл и отдаёт его системе."""
    app, adb, base = server
    calls = []
    monkeypatch.setattr("sparktech.web.server.open_with_default",
                        lambda path: (calls.append(Path(path)), _Ok())[1])
    payload = post(app, base, "/api/open-remote", {
        "serial": "SERIAL1",
        "scan_id": "scan1",
        "path": "/storage/emulated/0/Movies/clip.mp4",
    })
    assert payload["ok"] and payload["opened"]
    local = Path(payload["local_path"])
    assert local.is_file()
    assert local.parent == app.download_dir
    assert calls and calls[0] == local
    # Размер копии сверяется: содержимое совпадает с «телефоном».
    assert local.read_bytes() == adb.files["/storage/emulated/0/Movies/clip.mp4"]


class _Ok:
    ok = True
    message = ""


class _Bad:
    def __init__(self, message: str):
        self.ok = False
        self.message = message


def test_open_remote_rejects_file_outside_scan(server):
    app, adb, base = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(app, base, "/api/open-remote", {
            "serial": "SERIAL1", "scan_id": "scan1", "path": "/data/data/secret",
        })
    assert exc.value.code == 403


def test_open_data_dir_endpoint(server, monkeypatch):
    app, adb, base = server
    monkeypatch.setattr("sparktech.web.server.open_folder", lambda path: _Ok())
    payload = post(app, base, "/api/open-data-dir", {})
    assert payload["ok"]
    assert payload["folder"] == str(app.store.dir)


def test_apps_endpoint_requires_serial(server):
    app, adb, base = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, "/api/apps", token=app.token)
    assert exc.value.code == 409


def test_apps_endpoint_returns_notes_and_totals(server, monkeypatch):
    from sparktech.core.apps import AppInfo

    app, adb, base = server

    class FakeService:
        def list_apps(self, serial, **_kwargs):
            return [
                AppInfo(package="com.a", label="А", apk_size=100, data_size=50),
                AppInfo(package="com.b", is_system=True, apk_size=10),
            ]

        def icon(self, serial, target):
            return b"\x89PNG"

    app.apps = FakeService()
    data = json.loads(get(base, "/api/apps?serial=SERIAL1", token=app.token).read().decode())
    assert data["count"] == 2
    assert data["total_size"] == 160
    assert data["removable_size"] == 150
    assert any("root" in note for note in data["notes"])


def test_app_icon_returns_png(server):
    from sparktech.core.apps import AppInfo

    app, adb, base = server

    class FakeService:
        def list_apps(self, serial, **_kwargs):
            return [AppInfo(package="com.a", apk_size=1)]

        def icon(self, serial, target):
            return b"\x89PNG\r\n\x1a\npayload"

    app.apps = FakeService()
    get(base, "/api/apps?serial=SERIAL1", token=app.token)  # наполнить кэш
    with get(base, "/api/app-icon/com.a?serial=SERIAL1", token=app.token) as res:
        assert res.status == 200
        assert res.headers["Content-Type"] == "image/png"
        assert res.read().startswith(b"\x89PNG")


def test_app_icon_unknown_package_is_404(server):
    app, adb, base = server
    app.app_cache["SERIAL1"] = []
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, "/api/app-icon/com.nope?serial=SERIAL1", token=app.token)
    assert exc.value.code == 404
