"""Тесты разбора ресурсов APK (названия и иконки приложений).

Проверяется настоящая структура формата: ``tests/apk_builder.py`` собирает
``resources.arsc`` и бинарный ``AndroidManifest.xml`` по заголовкам
ResChunk/ResStringPool/ResTable_type/ResXMLTree. Если эти тесты зелёные,
SparkTech действительно показывает реальное название и иконку, а не имя
пакета с заглушкой.
"""

from __future__ import annotations

import struct

import pytest

from sparktech.core.arsc import (
    ApkResources,
    ResourceTable,
    ResourceTableError,
    StringPool,
    parse_manifest,
)

from apk_builder import build_apk, manifest_axml, resource_table, string_pool


def test_string_pool_utf8_roundtrip():
    pool = string_pool(["Привет", "world", "тест"])
    assert pool.startswith(struct.pack("<H", 0x0001))
    parsed = StringPool(pool, 0)
    assert [parsed.get(i) for i in range(parsed.count)] == ["Привет", "world", "тест"]


def test_string_pool_utf16_roundtrip():
    parsed = StringPool(string_pool(["Кириллица", "abc"], utf8=False), 0)
    assert [parsed.get(i) for i in range(parsed.count)] == ["Кириллица", "abc"]


def test_manifest_yields_label_and_icon_resources():
    info = parse_manifest(manifest_axml())
    assert info.package_name == "com.example.test"
    # Те самые идентификаторы android:label / android:icon из манифеста.
    assert info.label_res_id == 0x7F010000
    assert info.icon_res_id == 0x7F020000


def test_manifest_rejects_garbage():
    with pytest.raises(ResourceTableError):
        parse_manifest(b"not an axml file at all")


def test_resource_table_resolves_label_string():
    table = ResourceTable(resource_table(label="Мой мессенджер"))
    assert table.string_of(0x7F010000) == "Мой мессенджер"


def test_resource_table_resolves_icon_file_path():
    table = ResourceTable(resource_table(icon_path="res/mipmap-hdpi/ic_launcher.png"))
    assert table.file_of(0x7F020000) == "res/mipmap-hdpi/ic_launcher.png"


def test_resource_table_missing_entry_returns_none():
    table = ResourceTable(resource_table())
    assert table.value_of(0x7F030099) is None


def test_apk_resources_full_pipeline():
    """Главный тест: из APK получаем и название, и байты иконки."""
    resources = ApkResources.from_apk_intro(build_apk(label="WhatsApp Тест"))
    assert resources.application_label() == "WhatsApp Тест"
    assert resources.icon_file() == "res/mipmap/ic_launcher.png"
    assert resources.icon_bytes().startswith(b"\x89PNG")


def test_apk_without_manifest_falls_back_gracefully():
    resources = ApkResources.from_apk_intro(build_apk(with_manifest=False))
    assert resources.application_label() == ""
    assert resources.icon_bytes() == b""


def test_apk_without_icon_returns_empty_bytes():
    resources = ApkResources.from_apk_intro(build_apk(with_icon=False))
    assert resources.application_label() == "Тестовое приложение"
    assert resources.icon_bytes() == b""


def test_not_a_zip_is_rejected():
    with pytest.raises(ResourceTableError):
        ApkResources.from_apk_intro(b"\x7fELF not a zip")


def test_truncated_apk_does_not_crash():
    """Обрезанный APK (прочитали только начало) не должен ронять программу."""
    full = build_apk()
    for cut in (40, 120, len(full) // 2):
        try:
            resources = ApkResources.from_apk_intro(full[:cut])
        except ResourceTableError:
            continue
        # Если что-то разобралось — значения либо корректные, либо пустые.
        assert isinstance(resources.application_label(), str)


def test_broken_resource_table_is_rejected():
    with pytest.raises(ResourceTableError):
        ResourceTable(b"\x00" * 40)
