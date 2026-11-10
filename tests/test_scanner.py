"""Сканирование через фальшивый ADB: области, отмена, дубликаты, покрытие."""

import threading

from sparktech.core.classify import Settings
from sparktech.core.scanner import ScanOptions, Scanner, parse_find_script_command
from sparktech.core.models import RemoteFile


def make_scanner(fake):
    return Scanner(fake)


def test_scan_finds_files_and_reports_coverage(fake_adb, sample_files):
    fake_adb.files = dict(sample_files)
    progress = []
    result = make_scanner(fake_adb).scan(
        "TESTSERIAL", model="Pixel Test", progress=lambda stage, data: progress.append((stage, data))
    )
    paths = {i.path for i in result.items}
    assert "/storage/emulated/0/Download/setup.apk" in paths
    assert "/storage/emulated/0/Download/movie.mp4" in paths
    # OBB — защищённая область, в результаты не попадает
    assert not any("/Android/obb/" in p for p in paths)
    assert result.areas and all(a.status in ("scanned", "denied", "missing") for a in result.areas)
    assert result.storage_free and result.storage_total
    assert any("Закрытые данные приложений" in w for w in result.warnings)
    assert any(stage == "done" for stage, _ in progress)


def test_scan_reports_inaccessible_area(fake_adb, sample_files):
    fake_adb.files = dict(sample_files)
    fake_adb.fail_roots = {"/storage/emulated/0/DCIM"}
    result = make_scanner(fake_adb).scan("TESTSERIAL")
    areas = {a.path: a for a in result.areas}
    assert areas["/storage/emulated/0/DCIM"].status == "denied"
    assert any("не удалось прочитать" in w for w in result.warnings)


def test_scan_without_roots_raises(fake_adb):
    fake_adb.existing_roots = set()
    import pytest
    from sparktech.core.process import CommandError

    with pytest.raises(CommandError):
        make_scanner(fake_adb).scan("TESTSERIAL")


def test_scan_cancellation(fake_adb, sample_files):
    fake_adb.files = dict(sample_files)
    cancel = threading.Event()
    cancel.set()
    result = make_scanner(fake_adb).scan("TESTSERIAL", cancel_event=cancel)
    assert result.cancelled
    assert result.items == []


def test_scan_marks_duplicates_with_partial_hash(fake_adb):
    size = 2 * 1024 * 1024
    fake_adb.files = {
        "/storage/emulated/0/Download/one.bin": RemoteFile("/storage/emulated/0/Download/one.bin", size, 1),
        "/storage/emulated/0/DCIM/two.bin": RemoteFile("/storage/emulated/0/DCIM/two.bin", size, 1),
    }
    fake_adb.existing_roots = {"/storage/emulated/0/Download", "/storage/emulated/0/DCIM"}
    fake_adb.__dict__["_unused"] = {
        "/storage/emulated/0/Download/x": None
    }
    fake_adb.hashes = {
        "/storage/emulated/0/Download/one.bin": "c" * 64,
        "/storage/emulated/0/DCIM/two.bin": "c" * 64,
    }
    settings = Settings(big_file_threshold=10**12, duplicate_min_size=1024)
    result = make_scanner(fake_adb).scan("TESTSERIAL", options=ScanOptions(settings=settings))
    dup = [i for i in result.items if "duplicate" in i.categories]
    assert len(dup) == 1
    assert dup[0].duplicate_of == "/storage/emulated/0/DCIM/two.bin"


def test_scan_respects_max_items(fake_adb):
    fake_adb.files = {
        f"/storage/emulated/0/Download/f{i}.apk": RemoteFile(f"/storage/emulated/0/Download/f{i}.apk", 1000 + i, 1)
        for i in range(150)
    }
    settings = Settings(max_items=100, find_duplicates=False)
    result = make_scanner(fake_adb).scan("TESTSERIAL", options=ScanOptions(settings=settings))
    assert len(result.items) == 100
    assert any("показаны самые крупные" in w for w in result.warnings)


def test_find_command_quotes_root_safely():
    cmd = parse_find_script_command("/storage/emulated/0/Download")
    assert "'/storage/emulated/0/Download'" in cmd
    assert "%s" in cmd


def test_to_dict_is_json_friendly(fake_adb, sample_files):
    import json

    fake_adb.files = dict(sample_files)
    result = make_scanner(fake_adb).scan("TESTSERIAL", model="Pixel")
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "Pixel" in payload
    data = json.loads(payload)
    assert data["count"] == len(result.items)
    assert data["summary"]


def test_skip_statistics_are_reported(fake_adb):
    """Пользователь видит, сколько файлов не попало в результат и почему."""
    fake_adb.existing_roots = {
        "/storage/emulated/0/Download",
        "/storage/emulated/0/Android/data",  # добавим явно как доп. корень
    }
    fake_adb.files = {
        "/storage/emulated/0/Download/big.mp4": RemoteFile(
            "/storage/emulated/0/Download/big.mp4", 200 * 1024 * 1024, 1
        ),
        "/storage/emulated/0/Download/tiny.txt": RemoteFile(
            "/storage/emulated/0/Download/tiny.txt", 10, 1
        ),
        "/storage/emulated/0/Android/data/com.x/cache.bin": RemoteFile(
            "/storage/emulated/0/Android/data/com.x/cache.bin", 50 * 1024 * 1024, 1
        ),
    }
    result = make_scanner(fake_adb).scan(
        "TESTSERIAL",
        options=ScanOptions(
            # мелкие файлы (10 Б) отсекаются порогом 1 МБ — они попадут в «small»
            settings=Settings(min_size=1024 * 1024, find_duplicates=False),
            extra_roots=["/storage/emulated/0/Android/data"],
        ),
    )
    assert result.skipped["small"] == 1  # tiny.txt
    assert result.skipped["protected"] == 1  # Android/data
    assert [i.path for i in result.items] == ["/storage/emulated/0/Download/big.mp4"]
    assert result.to_dict()["skipped"] == {"protected": 1, "small": 1, "other": 0}


def test_min_size_skips_are_counted(fake_adb):
    fake_adb.files = {
        "/storage/emulated/0/Download/big.apk": RemoteFile(
            "/storage/emulated/0/Download/big.apk", 80 * 1024 * 1024, 1
        ),
        "/storage/emulated/0/Download/small.apk": RemoteFile(
            "/storage/emulated/0/Download/small.apk", 1024, 1
        ),
    }
    settings = Settings(min_size=10 * 1024 * 1024, find_duplicates=False)
    result = make_scanner(fake_adb).scan("TESTSERIAL", options=ScanOptions(settings=settings))
    assert result.skipped["small"] == 1
    assert [i.path for i in result.items] == ["/storage/emulated/0/Download/big.apk"]
