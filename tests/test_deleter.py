"""Безопасное удаление: запреты, проверка размера, отчёты, оболочка."""

from sparktech.core.deleter import Deleter, validate_batch
from sparktech.core.models import RemoteFile

ALLOWED = "/storage/emulated/0/Download/setup.apk"


def setup_file(fake, path=ALLOWED, size=1024):
    fake.files[path] = RemoteFile(path=path, size=size, mtime=1)


def test_validate_batch_rejects_system_paths():
    allowed, rejected = validate_batch([
        ALLOWED,
        "/system/bin/sh",
        "/data/data/com.x/db",
        "relative/path",
        "/storage/emulated/0/Download/a/../b",
    ])
    assert allowed == [ALLOWED]
    assert len(rejected) == 4
    assert all(not r.ok for r in rejected)


def test_validate_batch_deduplicates():
    allowed, _ = validate_batch([ALLOWED, ALLOWED])
    assert allowed == [ALLOWED]


def test_validate_batch_rejects_globs():
    allowed, rejected = validate_batch(["/storage/emulated/0/Download/*"])
    assert not allowed
    assert "Шаблоны" in rejected[0].message


def test_delete_one_success(fake_adb):
    setup_file(fake_adb)
    res = Deleter(fake_adb).delete_one("TESTSERIAL", ALLOWED, expected_size=1024)
    assert res.ok, res.message
    assert ALLOWED in fake_adb.deleted
    assert ALLOWED not in fake_adb.files


def test_delete_refuses_when_size_changed(fake_adb):
    setup_file(fake_adb, size=1024)
    fake_adb.changed_size[ALLOWED] = 2048
    res = Deleter(fake_adb).delete_one("TESTSERIAL", ALLOWED, expected_size=1024)
    assert not res.ok
    assert "изменился" in res.message or "отличается" in res.message
    assert not fake_adb.deleted


def test_delete_missing_file(fake_adb):
    res = Deleter(fake_adb).delete_one("TESTSERIAL", ALLOWED, expected_size=1)
    assert not res.ok
    assert "отсутствует" in res.message


def test_delete_failure_reported(fake_adb):
    """adb shell rm может вернуть 0 при отказе — проверяем верификацию."""
    setup_file(fake_adb)
    fake_adb.delete_fails.add(ALLOWED)
    # имитируем «тихий» отказ: файл остаётся на месте
    res = Deleter(fake_adb).delete_one("TESTSERIAL", ALLOWED)
    assert not res.ok
    assert "не выполнено" in res.message.lower()


def test_delete_many_partial_report(fake_adb):
    a, b = ALLOWED, "/storage/emulated/0/Download/b.zip"
    setup_file(fake_adb, a)
    setup_file(fake_adb, b)
    fake_adb.delete_fails.add(b)
    batch = Deleter(fake_adb).delete_many("TESTSERIAL", [a, b, "/system/bin/sh"], verify_size=False)
    assert len(batch.succeeded) == 1
    assert len(batch.failed) == 2  # отказ + защищённый путь
    report = batch.to_dict()
    assert report["deleted_count"] == 1
    assert report["failed_count"] == 2


def test_delete_many_cancellable(fake_adb):
    import threading

    paths = [f"/storage/emulated/0/Download/f{i}.apk" for i in range(5)]
    for p in paths:
        setup_file(fake_adb, p)
    cancel = threading.Event()
    cancel.set()
    batch = Deleter(fake_adb).delete_many("TESTSERIAL", paths, cancel_event=cancel, verify_size=False)
    assert batch.to_dict()["cancelled"] is True or len(batch.succeeded) == 0
    assert not fake_adb.deleted


def test_delete_special_characters(fake_adb):
    path = "/storage/emulated/0/Download/it's a $(name) ;x.apk"
    setup_file(fake_adb, path)
    res = Deleter(fake_adb).delete_one("TESTSERIAL", path, verify_size=False)
    assert res.ok, res.message
    assert path in fake_adb.deleted
    assert not fake_adb.files


def test_delete_never_targets_directory(fake_adb):
    # /storage/emulated/0/Download не считается файлом
    res = Deleter(fake_adb).delete_one("TESTSERIAL", "/storage/emulated/0/Download")
    assert not res.ok


def test_successful_delete_reports_verified_size(fake_adb):
    setup_file(fake_adb, ALLOWED, size=4096)
    batch = Deleter(fake_adb).delete_many("TESTSERIAL", [ALLOWED], sizes={ALLOWED: 4096})
    report = batch.to_dict()
    assert report["freed_bytes"] == 4096
    assert report["deleted"][0]["size"] == 4096
