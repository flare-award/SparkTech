"""Анализ результатов сканирования: категории, причины, осторожность, дубликаты."""

from sparktech.core.classify import (
    Settings,
    apply_duplicates,
    classify_all,
    classify_file,
    duplicate_candidates,
    filter_items,
    is_in_protected_dir,
)
from sparktech.core.models import RemoteFile

SETTINGS = Settings(big_file_threshold=100 * 1024 * 1024)


def rf(path: str, size: int = 1024, mtime: int = 1700000000) -> RemoteFile:
    return RemoteFile(path=path, size=size, mtime=mtime)


def test_big_file_has_reason_and_not_called_garbage():
    item = classify_file(rf("/storage/emulated/0/Movies/video.mp4", 500 * 1024 * 1024), SETTINGS)
    assert item is not None
    assert "big" in item.categories
    assert any("превышает порог" in r for r in item.reasons)
    joined = " ".join(item.reasons).lower()
    assert "мусор" in joined  # прямо написано, что размер не делает файл мусором


def test_threshold_respected():
    small = rf("/storage/emulated/0/Documents/small.mp4", 10 * 1024 * 1024)
    assert classify_file(small, SETTINGS) is None


def test_apk_category():
    item = classify_file(rf("/storage/emulated/0/Download/app.apk", 20 * 1024 * 1024), SETTINGS)
    assert "apk" in item.categories and "download" in item.categories


def test_archive_category():
    item = classify_file(rf("/storage/emulated/0/Download/backup.zip", 5 * 1024 * 1024), SETTINGS)
    assert "archive" in item.categories


def test_apk_can_be_disabled():
    s = Settings(include_apk=False, include_downloads=False, big_file_threshold=10**12)
    assert classify_file(rf("/storage/emulated/0/Download/app.apk", 5 * 1024 * 1024), s) is None


def test_temp_file_in_cache_warns_about_working_files():
    item = classify_file(rf("/storage/emulated/0/Download/render.tmp", 1024), SETTINGS)
    assert "temp" in item.categories
    assert item.risk == "high"
    assert any("рабочие файлы" in w for w in item.extra_warnings)


def test_sensitive_directory_raises_risk():
    item = classify_file(rf("/storage/emulated/0/DCIM/Camera/IMG_1.jpg", 200 * 1024 * 1024), SETTINGS)
    assert item.risk == "high"
    assert any("медиа" in w for w in item.extra_warnings)


def test_protected_app_dirs_never_suggested():
    # OBB-файлы показываются отдельно (см. test_obb_files_shown_but_marked_protected),
    # но никогда не предлагаются к удалению.
    for path in (
        "/storage/emulated/0/Android/data/com.app/cache/x.tmp",
        "/storage/emulated/0/Android/media/com.app/x.mp4",
    ):
        assert is_in_protected_dir(path)
        assert classify_file(rf(path, 900 * 1024 * 1024), SETTINGS) is None
    obb = classify_file(rf("/storage/emulated/0/Android/obb/com.game/main.obb", 900 * 1024 * 1024), SETTINGS)
    assert obb is not None and obb.risk == "high"


def test_min_size_filter():
    s = Settings(big_file_threshold=100 * 1024 * 1024, min_size=50 * 1024 * 1024)
    assert classify_file(rf("/storage/emulated/0/Download/a.apk", 10 * 1024 * 1024), s) is None
    assert classify_file(rf("/storage/emulated/0/Download/b.apk", 60 * 1024 * 1024), s) is not None


def test_to_dict_contains_explanations():
    item = classify_file(rf("/storage/emulated/0/Download/app.apk", 60 * 1024 * 1024), SETTINGS)
    d = item.to_dict()
    assert d["reasons"] and d["category_key"] == "apk"
    assert d["risk_label"] in ("Низкая осторожность", "Средняя осторожность", "Высокая осторожность")


def test_duplicate_candidates_only_same_size_above_min():
    files = [
        rf("/storage/emulated/0/Download/a.bin", 2 * 1024 * 1024),
        rf("/storage/emulated/0/DCIM/b.bin", 2 * 1024 * 1024),
        rf("/storage/emulated/0/Download/c.bin", 3 * 1024 * 1024),
        rf("/storage/emulated/0/Download/small.bin", 100),
        rf("/storage/emulated/0/Download/small2.bin", 100),
    ]
    cands = duplicate_candidates(files, SETTINGS)
    assert len(cands) == 1
    assert [f.path for f in cands[0].files] == [
        "/storage/emulated/0/DCIM/b.bin",
        "/storage/emulated/0/Download/a.bin",
    ]
    assert cands[0].hash_kind == "partial"


def test_apply_duplicates_marks_copy_not_original():
    files = [
        rf("/storage/emulated/0/Download/orig.zip", 50 * 1024 * 1024),
        rf("/storage/emulated/0/DCIM/copy.zip", 50 * 1024 * 1024),
    ]
    items = classify_all(files, SETTINGS)
    hashes = {
        "/storage/emulated/0/Download/orig.zip": "a" * 64,
        "/storage/emulated/0/DCIM/copy.zip": "a" * 64,
    }
    apply_duplicates(items, hashes, "partial")
    by_path = {i.path: i for i in items}
    assert "duplicate" in items[1].categories or "duplicate" in items[0].categories
    original = by_path["/storage/emulated/0/DCIM/copy.zip"]  # алфавитно первый
    assert "duplicate" not in original.categories
    dup = by_path["/storage/emulated/0/Download/orig.zip"]
    assert "duplicate" in dup.categories
    assert dup.duplicate_of == original.path
    assert any(("частичн" in w or "Дубликат определён" in w) for w in dup.extra_warnings)


def test_apply_duplicates_ignores_different_hashes():
    files = [rf("/storage/emulated/0/Download/a.zip", 2 * 1024 * 1024),
             rf("/storage/emulated/0/Download/b.zip", 2 * 1024 * 1024)]
    items = classify_all(files, SETTINGS)
    apply_duplicates(items, {
        "/storage/emulated/0/Download/a.zip": "a" * 64,
        "/storage/emulated/0/Download/b.zip": "b" * 64,
    }, "full")
    assert not any("duplicate" in i.categories for i in items)


def test_settings_from_payload_validation():
    s = Settings.from_payload({"big_file_threshold": "not-a-number", "max_items": 999999,
                               "include_apk": "false", "min_size": -5})
    assert s.big_file_threshold == Settings().big_file_threshold
    assert s.max_items == 20000
    assert s.include_apk is False
    assert s.min_size == 0


def test_filter_and_sort_items():
    files = [
        rf("/storage/emulated/0/Download/b.apk", 10),
        rf("/storage/emulated/0/Download/a.apk", 30),
        rf("/storage/emulated/0/Download/c.mp4", 20, mtime=1),
    ]
    items = classify_all(files, Settings(big_file_threshold=25))
    sorted_items = filter_items(items, sort="size", descending=True)
    assert [i.size for i in sorted_items] == [30, 20, 10]
    assert [i.size for i in filter_items(items, sort="size", descending=False)] == [10, 20, 30]
    assert len(filter_items(items, query="a.apk")) == 1
    assert len(filter_items(items, categories={"big"})) == 1  # порог 25 Б: только a.apk
    assert len(filter_items(items, risks={"medium", "high"})) == len(items)


def test_original_is_labelled_but_not_suggested_for_deletion():
    files = [
        rf("/storage/emulated/0/Download/report.pdf", 5 * 1024 * 1024),
        rf("/storage/emulated/0/Download/report (1).pdf", 5 * 1024 * 1024),
    ]
    items = classify_all(files, SETTINGS)
    assert len(items) == 2, "оба файла должны попасть в результат (папка загрузок)"
    hashes = {i.path: "f" * 64 for i in items}
    apply_duplicates(items, hashes, "full")
    original = next(i for i in items if i.path.endswith("report (1).pdf"))
    copy = next(i for i in items if i.path.endswith("/report.pdf"))
    assert "original" in original.categories
    assert original.duplicate_of is None
    assert copy.duplicate_of == original.path
    assert any("не предлагает удалять" in r for r in original.reasons)


def test_obb_files_shown_but_marked_protected():
    item = classify_file(
        rf("/storage/emulated/0/Android/obb/com.game/main.obb", 900 * 1024 * 1024), SETTINGS
    )
    assert item is not None
    assert item.risk == "high"
    assert any("не удаляет" in w for w in item.extra_warnings)
    # и в дубликаты такие пути как «оригиналы» попадать могут
    files = [rf("/storage/emulated/0/Android/obb/com.game/main.obb", 3 * 1024 * 1024),
             rf("/storage/emulated/0/Download/main.obb", 3 * 1024 * 1024)]
    assert len(duplicate_candidates(files, SETTINGS)) == 1
