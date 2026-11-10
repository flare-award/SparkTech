"""Интеграционный тест через настоящий процесс: SparkTech ↔ mock adb ↔ POSIX sh.

В отличие от остальных тестов здесь используется боевой транспорт
(``process.run`` / ``process.run_streaming``), настоящая оболочка и настоящий
`find`/`rm` — на макете файловой системы телефона
(``scripts/mock_adb.py``). Так проверяется вся цепочка: экранирование,
потоковый разбор вывода, удаление и повторная проверка результата.

Тест пропускается на Windows: макет использует POSIX-оболочку.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from sparktech.core.adb import AdbClient
from sparktech.core.classify import Settings
from sparktech.core.deleter import Deleter
from sparktech.core.models import RemoteFile
from sparktech.core.optimizer import Optimizer
from sparktech.core.scanner import ScanOptions, Scanner

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="макет использует POSIX-оболочку"
)

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "scripts" / "mock_adb.py"
PHONE = "/storage/emulated/0"


class DictStore:
    def __init__(self):
        self.saved = {}

    def save_setting_origin(self, serial, key, value):
        self.saved.setdefault((serial, key), value)

    def get_saved_setting(self, serial, key):
        return self.saved.get((serial, key))

    def saved_settings(self, serial):
        return {k[1]: v for k, v in self.saved.items() if k[0] == serial}

    def clear_saved_setting(self, serial, key):
        self.saved.pop((serial, key), None)


@pytest.fixture
def phone(tmp_path, monkeypatch):
    """Макет файловой системы телефона + обёртка вместо adb.exe."""
    mock_root = tmp_path / "phone"
    for sub in ("Download", "DCIM/Camera", "Documents", "Android/obb/com.game", "Android/data/com.x"):
        (mock_root / sub).mkdir(parents=True)

    big = mock_root / "Download" / "video big file.mp4"
    with open(big, "wb") as fh:
        fh.truncate(200 * 1024 * 1024)
    with open(mock_root / "Download" / "app.apk", "wb") as fh:
        fh.truncate(30 * 1024 * 1024)
    tricky = mock_root / "Download" / "it's files" / "$(reboot); name.zip"
    tricky.parent.mkdir()
    tricky.write_text("x", encoding="utf-8")
    with open(mock_root / "DCIM" / "Camera" / "IMG_1.jpg", "wb") as fh:
        fh.truncate(120 * 1024 * 1024)
    with open(mock_root / "Android" / "obb" / "com.game" / "main.obb", "wb") as fh:
        fh.truncate(300 * 1024 * 1024)
    dup_a = mock_root / "Documents" / "a.bin"
    with open(dup_a, "wb") as fh:
        fh.write(b"same content" * 1000)

    # запускаемый скрипт-обёртка: SparkTech вызывает его как adb
    wrapper = tmp_path / "adb"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{MOCK}" "$@"\n', encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("SPARKTECH_MOCK_ROOT", str(mock_root))
    monkeypatch.setenv("SPARKTECH_MOCK_SERIAL", "MOCKTEST01")
    settings_file = MOCK.parent / ".mock_settings"
    if settings_file.exists():
        settings_file.unlink()
    yield {"root": mock_root, "adb": str(wrapper), "settings_file": settings_file}
    if settings_file.exists():
        settings_file.unlink()


@pytest.fixture
def client(phone):
    return AdbClient(phone["adb"], timeout=60)


def test_adb_sees_mock_device(client):
    devices = client.list_devices()
    assert len(devices) == 1
    assert devices[0].serial == "MOCKTEST01"
    assert devices[0].ready
    assert devices[0].model == "Mock Pixel 7"
    assert devices[0].android_version == "13"


def test_real_scan_pipeline(client, phone):
    scanner = Scanner(client)
    result = scanner.scan(
        "MOCKTEST01",
        model="Mock Pixel 7",
        options=ScanOptions(
            settings=Settings(big_file_threshold=100 * 1024 * 1024, find_duplicates=False),
            include_obb=True,
        ),
    )
    paths = {i.path for i in result.items}
    assert f"{PHONE}/Download/video big file.mp4" in paths
    assert f"{PHONE}/Download/app.apk" in paths
    assert f"{PHONE}/Download/it's files/$(reboot); name.zip" in paths
    assert f"{PHONE}/DCIM/Camera/IMG_1.jpg" in paths
    assert f"{PHONE}/Android/obb/com.game/main.obb" in paths
    # закрытые каталоги приложений не показываются
    assert not any("/Android/data/" in p for p in paths)
    assert result.storage_free and result.storage_total
    assert any(a.status == "scanned" for a in result.areas)

    obb = next(i for i in result.items if i.path.endswith("main.obb"))
    assert obb.risk == "high"
    photo = next(i for i in result.items if i.path.endswith("IMG_1.jpg"))
    assert photo.risk == "high"

    # OBB удалять нельзя даже при включённом показе
    allowed, reason = __import__(
        "sparktech.core.remote_path", fromlist=["is_safe_deletable"]
    ).is_safe_deletable(obb.path)
    assert not allowed


def test_real_delete_with_special_characters(client, phone):
    deleter = Deleter(client)
    path = f"{PHONE}/Download/it's files/$(reboot); name.zip"
    res = deleter.delete_one("MOCKTEST01", path, expected_size=1)
    assert res.ok, res.message
    assert not (phone["root"] / "Download" / "it's files" / "$(reboot); name.zip").exists()
    # ничего лишнего не удалено
    assert (phone["root"] / "Download" / "app.apk").exists()


def test_real_delete_refuses_protected_paths(client, phone):
    deleter = Deleter(client)
    for path in (
        f"{PHONE}/Android/obb/com.game/main.obb",
        f"{PHONE}/Android/data/com.x",
        "/system/bin/sh",
    ):
        res = deleter.delete_one("MOCKTEST01", path)
        assert not res.ok
    assert (phone["root"] / "Android" / "obb" / "com.game" / "main.obb").exists()


def test_real_delete_detects_size_change(client, phone):
    target = phone["root"] / "Download" / "app.apk"
    deleter = Deleter(client)
    # размер на «устройстве» отличается от размера при сканировании
    res = deleter.delete_one("MOCKTEST01", f"{PHONE}/Download/app.apk", expected_size=123)
    assert not res.ok
    assert "отличается" in res.message
    assert target.exists()


def test_real_optimizer_confirms_settings(client, phone):
    store = DictStore()
    optimizer = Optimizer(client, store)
    outcome = optimizer.run("MOCKTEST01", "window_animation", sdk=33)
    assert outcome.status == "success"
    saved = store.get_saved_setting("MOCKTEST01", "window_animation_scale")
    assert saved in ("1", "null") or saved is not None
    undo = optimizer.undo("MOCKTEST01", "window_animation")
    assert undo.status == "success"


def test_unavailable_adb_path_reports_clearly():
    client = AdbClient("/nonexistent/adb-binary")
    from sparktech.core.adb import AdbNotFound

    with pytest.raises(AdbNotFound):
        client.resolve(force=True)
