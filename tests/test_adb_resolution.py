"""Диагностика ADB: понятные причины вместо догадок и работа с путём-папкой.

Эти тесты появились из реального случая: пользователь скачал официальный
Platform-Tools, указал путь, но увидел «adb.exe найден, но не отвечает на
запрос версии. Возможно, файл повреждён…». Причина была другой: в настройки
попадала папка, а не файл, и интерфейс вместо факта показывал догадку.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from sparktech.core.adb import AdbClient, AdbNotFound


def make_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


# --------------------------------------------------------------------- пути


def test_folder_path_is_resolved_to_adb_inside(tmp_path):
    """Пользователь вставляет папку platform-tools — находим adb внутри."""
    tools = tmp_path / "platform-tools"
    adb = make_script(tools / "adb", "#!/bin/sh\necho 'Android Debug Bridge version 1.0.41'\n")
    client = AdbClient(str(tools))
    assert client.normalize_user_path(str(tools)) == str(adb)
    assert client.resolve() == str(adb)


def test_sdk_root_path_is_resolved(tmp_path):
    """Можно указать весь Android SDK — adb ищется в platform-tools."""
    sdk = tmp_path / "Sdk"
    adb = make_script(sdk / "platform-tools" / "adb", "#!/bin/sh\necho ok\n")
    client = AdbClient(str(sdk))
    assert client.normalize_user_path(str(sdk)) == str(adb)


def test_quotes_and_whitespace_are_stripped(tmp_path):
    adb = make_script(tmp_path / "platform-tools" / "adb", "#!/bin/sh\necho ok\n")
    client = AdbClient("")
    assert client.normalize_user_path(f'  "{adb}"  ') == str(adb)
    assert client.normalize_user_path(f"'{adb}'") == str(adb)


def test_path_with_spaces_works(tmp_path):
    folder = tmp_path / "Program Files" / "platform-tools"
    adb = make_script(folder / "adb", "#!/bin/sh\necho ok\n")
    client = AdbClient(str(folder))
    assert client.resolve() == str(adb)


def test_folder_alone_does_not_count_as_executable(tmp_path):
    """Папка не должна приниматься за adb: именно это давало ложное сообщение."""
    folder = tmp_path / "platform-tools"
    folder.mkdir()
    client = AdbClient(str(folder))
    with pytest.raises(AdbNotFound):
        client.resolve(force=True)


def test_nonexistent_path_reports_not_found(tmp_path):
    client = AdbClient(str(tmp_path / "nope" / "adb.exe"))
    with pytest.raises(AdbNotFound):
        client.resolve(force=True)


# -------------------------------------------------------------- диагностика


def test_version_reports_real_permission_error(tmp_path):
    """Настоящая причина: файл не исполняется, а не «повреждён»."""
    if os.name == "nt":
        pytest.skip("права POSIX")
    plain = tmp_path / "adb"
    plain.write_text("#!/bin/sh\necho 'Android Debug Bridge version 1.0.41'\n")
    plain.chmod(0o644)  # без бита исполнения
    version, problem = AdbClient(str(plain)).version_diagnostics()
    assert version is None
    assert "прав" in (problem or "").lower() or "не удалось" in (problem or "").lower()


def test_version_reports_output_of_wrong_program(tmp_path):
    """Запускается, но это не adb — показываем реальный вывод."""
    fake = make_script(tmp_path / "adb", "#!/bin/sh\necho 'This app cannot run on your PC' >&2\nexit 1\n")
    version, problem = AdbClient(str(fake)).version_diagnostics()
    assert version is None
    assert "This app cannot run" in problem
    assert "код 1" in problem


def test_version_reports_empty_output(tmp_path):
    fake = make_script(tmp_path / "adb", "#!/bin/sh\nexit 0\n")
    version, problem = AdbClient(str(fake)).version_diagnostics()
    assert version is None
    assert "не сообщила версию" in problem


def test_version_parses_official_output():
    class FakeVer:
        adb_path = "/fake/adb"

        def run(self, args, **kwargs):
            from sparktech.core.process import CommandResult

            return CommandResult(
                argv=["adb", "version"], returncode=0,
                stdout="Android Debug Bridge version 1.0.41\nVersion 35.0.2-12147458\n",
                stderr="",
            )

    version, problem = AdbClient.version_diagnostics(FakeVer())
    assert version == "1.0.41"
    assert problem is None


def test_diagnostics_never_raise(tmp_path):
    """Даже если adb «завис» или упал, наружу уходит текст, а не исключение."""
    class Boom:
        adb_path = "/fake/adb"

        def run(self, args, **kwargs):
            from sparktech.core.process import CommandTimeout

            raise CommandTimeout("Команда не ответила за 15 с")

    version, problem = AdbClient.version_diagnostics(Boom())
    assert version is None
    assert "15 секунд" in problem


def test_server_reports_actual_problem(tmp_path, monkeypatch):
    """API не должен подменять реальную ошибку догадкой про «повреждённый файл»."""
    from sparktech.core.store import Store
    from sparktech.web.server import AppContext, create_server

    app = AppContext(store=Store(tmp_path))

    class BrokenAdb:
        adb_path = str(tmp_path / "adb.exe")

        def resolve(self, force=False):
            return self.adb_path

        def version_diagnostics(self):
            return None, "Программа запустилась, но вернула код 1 и не похожа на adb. Вывод: This app cannot run on your PC"

        def set_path(self, path):
            pass

        def start_server(self):
            pass

        def list_devices(self, with_props=True):
            return []

        def get_props(self, serial):
            return {}

    app.adb = BrokenAdb()
    server = create_server(app)
    import threading
    import urllib.request
    import json

    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/devices",
            headers={"X-SparkTech-Token": app.token},
        )
        with urllib.request.urlopen(req, timeout=15) as res:
            payload = json.loads(res.read().decode())
        assert payload["status"] == "adb_error"
        assert "This app cannot run on your PC" in payload["adb"]["error"]
        assert "повреждён" not in payload["adb"]["error"]
        assert payload["adb"]["hint"]
    finally:
        server.shutdown()
        server.server_close()


def test_bad_saved_path_does_not_override_working_adb(tmp_path, monkeypatch):
    """Плохой путь не должен «залипать» и перекрывать автопоиск рабочего adb.

    Сценарий из жизни: пользователь один раз ввёл неверный путь — и после
    этого SparkTech сообщал об ошибке, хотя рядом с exe лежал рабочий adb.
    """
    good = make_script(tmp_path / "platform-tools" / "adb", "#!/bin/sh\necho ok\n")
    client = AdbClient(str(tmp_path / "broken" / "adb.exe"))
    client.set_path(str(tmp_path / "broken" / "adb.exe"))  # файла нет
    monkeypatch.setattr(AdbClient, "candidate_paths", staticmethod(lambda: [good]))
    assert client.resolve() == str(good)


def test_set_path_accepts_folder_and_stores_file(tmp_path):
    tools = tmp_path / "platform-tools"
    adb = make_script(tools / "adb", "#!/bin/sh\necho ok\n")
    client = AdbClient("")
    client.set_path(f'"{tools}"')
    assert client.resolve() == str(adb)


def test_force_resolve_keeps_user_path(tmp_path):
    """`resolve(force=True)` терял путь, указанный пользователем.

    Из-за этого при сохранении настроек SparkTech отвечал «adb не найден»,
    хотя путь верный, а интерфейс затем показывал «файл повреждён».
    """
    adb = make_script(tmp_path / "platform-tools" / "adb", "#!/bin/sh\necho ok\n")
    client = AdbClient(str(adb))
    assert client.resolve(force=True) == str(adb)
    # путь-папка тоже должен выживать при force
    client2 = AdbClient(str(adb.parent))
    assert client2.resolve(force=True) == str(adb)


def test_api_reports_bad_path_but_keeps_working_adb(tmp_path):
    """Неверный путь даёт понятную ошибку и не ломает уже рабочий adb.

    Иначе после одной опечатки SparkTech начинал показывать «ADB не
    запускается», хотя рабочий adb уже был найден.
    """
    import json
    import threading
    import urllib.request

    from sparktech.core.store import Store
    from sparktech.web.server import AppContext, create_server

    app = AppContext(store=Store(tmp_path))
    good = make_script(tmp_path / "platform-tools" / "adb", "#!/bin/sh\necho 'Android Debug Bridge version 1.0.41'\n")
    app.adb = AdbClient(str(good))
    server = create_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {"X-SparkTech-Token": app.token, "Content-Type": "application/json"}

    def post(path_value: str) -> dict:
        req = urllib.request.Request(
            base + "/api/settings",
            data=json.dumps({"adb_path": path_value}).encode(),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode())

    try:
        ok = post(f'"{good.parent}"')  # папка в кавычках, как из проводника
        assert ok["adb_resolved"] == str(good), ok
        assert not ok["adb_problem"]

        bad = post(str(tmp_path / "нет-такого"))
        assert bad["adb_resolved"] is None
        assert "не найден" in bad["adb_problem"]

        # рабочий adb не потерян
        assert app.adb.resolve() == str(good)
    finally:
        server.shutdown()
        server.server_close()
