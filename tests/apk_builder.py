"""Сборка маленького, но настоящего APK для тестов разбора ресурсов.

Зачем: проверять парсер ``resources.arsc``/AXML нужно на реальной структуре
формата, а не на «похожих байтах». Этот конструктор пишет минимум:

* ``AndroidManifest.xml`` — бинарный AXML с ``<manifest package>`` и
  ``<application android:label android:icon>``, где значения — ссылки на
  ресурсы (как в настоящих APK);
* ``resources.arsc`` — таблица с пакетом ``0x7f``, типами ``string`` и
  ``mipmap``, одной конфигурацией по умолчанию;
* файл иконки ``res/mipmap/ic_launcher.png``.

Формат соблюдён по заголовкам (ResChunk_header, ResStringPool, ResTable_package,
ResTable_type, Res_value, AXML ResXMLTree_node), поэтому тест поймает
регрессию в парсере, а не только в конструкторе.
"""

from __future__ import annotations

import io
import struct
import zipfile

RES_STRING_POOL_TYPE = 0x0001
RES_TABLE_TYPE = 0x0002
RES_TABLE_PACKAGE_TYPE = 0x0200
RES_TABLE_TYPE_TYPE = 0x0201
RES_TABLE_TYPE_SPEC_TYPE = 0x0202

AXML_FILE_TYPE = 0x0003
AXML_STRING_POOL_TYPE = 0x0001
AXML_RESOURCE_MAP_TYPE = 0x0180
AXML_START_ELEMENT_TYPE = 0x0102

TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03

ANDROID_NS = 0x01000000
NO_ENTRY = 0xFFFFFFFF


def string_pool(items: list[str], *, utf8: bool = True) -> bytes:
    """Собрать ``ResStringPool`` (UTF-8 или UTF-16)."""
    offsets: list[int] = []
    data = bytearray()
    for item in items:
        offsets.append(len(data))
        if utf8:
            encoded = item.encode("utf-8")
            # В UTF-8-пуле две длины: в символах и в байтах.
            data.extend(_len8(len(item)))
            data.extend(_len8(len(encoded)))
            data.extend(encoded)
            data.append(0)
        else:
            encoded = item.encode("utf-16-le")
            data.extend(_len16(len(item)))
            data.extend(encoded)
            data.extend(b"\x00\x00")
    while len(data) % 4:
        data.append(0)
    header_size = 28
    strings_start = header_size + 4 * len(items)
    body = struct.pack(f"<{len(offsets)}I", *offsets) + bytes(data)
    size = header_size + len(body)
    flags = 0x100 if utf8 else 0x000
    header = struct.pack(
        "<HHIIIIII", RES_STRING_POOL_TYPE, header_size, size, len(items), 0, flags, strings_start, 0
    )
    return header + body


def _len8(value: int) -> bytes:
    if value > 0x7F:
        return bytes([0x80 | (value >> 8), value & 0xFF])
    return bytes([value])


def _len16(value: int) -> bytes:
    if value > 0x7FFF:
        return struct.pack("<HH", 0x8000 | (value >> 16), value & 0xFFFF)
    return struct.pack("<H", value)


def _type_spec(type_id: int, entry_count: int) -> bytes:
    header_size = 16
    size = header_size + 4 * entry_count
    return (
        struct.pack("<HHIBBH", RES_TABLE_TYPE_SPEC_TYPE, header_size, size, type_id, 0, 0)
        + struct.pack("<I", entry_count)
        + b"\x00" * (4 * entry_count)
    )


def _type_chunk(type_id: int, entries: list[tuple[int, int] | None], key_index: int = 0) -> bytes:
    """Собрать ``ResTable_type`` с одной конфигурацией по умолчанию.

    ``entries`` — список ``(data_type, data)`` по индексам записей; ``None`` —
    запись отсутствует (NO_ENTRY).
    """
    config = struct.pack("<I", 64) + b"\x00" * 60  # ResTable_config, size = 64
    # Заголовок ResTable_type: ResChunk(8) + id(1) + flags(1) + reserved(2)
    # + entryCount(4) + entriesStart(4) + config(64) = 84 байта.
    header_size = 8 + 4 + 4 + 4 + len(config)
    entry_count = len(entries)

    offsets: list[int] = []
    body = bytearray()
    for entry in entries:
        if entry is None:
            offsets.append(NO_ENTRY)
            continue
        offsets.append(len(body))
        data_type, data = entry
        # ResTable_entry (8 байт) + Res_value (8 байт): размер 8, флаги 0, ключ.
        body.extend(struct.pack("<HHI", 8, 0, key_index))
        # Res_value: size(2) res0(1) dataType(1) data(4)
        body.extend(struct.pack("<HBBI", 8, 0, data_type, data))
    # entriesStart — смещение от начала блока (а не от конца заголовка).
    entries_start = header_size + 4 * entry_count
    size = entries_start + len(body)
    header = struct.pack(
        "<HHIBBHII",
        RES_TABLE_TYPE_TYPE,
        header_size,
        size,
        type_id,
        0,  # flags: обычный список смещений
        0,
        entry_count,
        entries_start,
    ) + config
    offsets_bytes = struct.pack(f"<{len(offsets)}I", *offsets) if offsets else b""
    return header + offsets_bytes + bytes(body)


def resource_table(
    *,
    package_id: int = 0x7F,
    label: str = "Тестовое приложение",
    icon_path: str = "res/mipmap/ic_launcher.png",
    label_index: int = 0,
    icon_index: int = 0,
) -> bytes:
    """Собрать ``resources.arsc``: string/app_name и mipmap/ic_launcher."""
    global_strings = string_pool([label, icon_path])
    type_strings = string_pool(["string", "mipmap"])
    key_strings = string_pool(["app_name", "ic_launcher"])

    chunks = (
        _type_spec(1, label_index + 1)
        + _type_chunk(1, [None] * label_index + [(TYPE_STRING, 0)])
        + _type_spec(2, icon_index + 1)
        + _type_chunk(2, [None] * icon_index + [(TYPE_STRING, 1)])
    )

    package_header_size = 288  # ResTable_package: id(4) + name(256) + прочее
    type_offsets = package_header_size
    key_offsets = type_offsets + len(type_strings)
    package = bytearray()
    package.extend(struct.pack("<HHI", RES_TABLE_PACKAGE_TYPE, package_header_size, 0))
    package.extend(struct.pack("<I", package_id))
    package.extend(b"\x00" * 256)  # name[128] в UTF-16
    package.extend(
        struct.pack("<IIIII", type_offsets, 0, key_offsets, 0, 0)
    )
    assert len(package) == package_header_size, len(package)
    package.extend(type_strings)
    package.extend(key_strings)
    package.extend(chunks)
    struct.pack_into("<I", package, 4, len(package))

    total = 12 + len(global_strings) + len(package)
    header = struct.pack("<HHII", RES_TABLE_TYPE, 12, total, 1)
    return header + global_strings + bytes(package)


def manifest_axml(
    *,
    package_name: str = "com.example.test",
    label_res_id: int = 0x7F010000,
    icon_res_id: int = 0x7F020000,
) -> bytes:
    """Собрать бинарный ``AndroidManifest.xml`` с ``<manifest>`` и ``<application>``."""
    names = [
        "android",          # 0
        "label",            # 1
        "icon",             # 2
        "application",      # 3
        "package",          # 4
        "manifest",         # 5
        package_name,       # 6
        "versionName",      # 7
        "1.2.3",            # 8
    ]
    resource_map = [0] * len(names)
    resource_map[1] = 0x01010001  # android:label
    resource_map[2] = 0x01010002  # android:icon

    map_chunk = struct.pack("<HHI", AXML_RESOURCE_MAP_TYPE, 8, 8 + 4 * len(resource_map))
    map_chunk += struct.pack(f"<{len(resource_map)}I", *resource_map)

    manifest_el = _start_element(5, [(NO_ENTRY, 4, TYPE_STRING, 6, None)])
    application_el = _start_element(
        3,
        [
            (NO_ENTRY, 1, TYPE_REFERENCE, label_res_id, 0x01010001),
            (NO_ENTRY, 2, TYPE_REFERENCE, icon_res_id, 0x01010002),
            (NO_ENTRY, 7, TYPE_STRING, 8, None),  # посторонний атрибут — не должен мешать
        ],
    )
    end_manifest = _end_element(5)
    end_application = _end_element(3)

    body = string_pool(names) + map_chunk + manifest_el + application_el + end_application + end_manifest
    return struct.pack("<HHI", AXML_FILE_TYPE, 8, 8 + len(body)) + body


def _start_element(
    name_index: int,
    attributes: list[tuple[int, int, int, int, int | None]],
) -> bytes:
    """``ResXMLTree_node`` + ``ResXMLTree_attrExt`` + атрибуты."""
    ext = struct.pack("<IIHHHHHH", NO_ENTRY, name_index, 20, 20, len(attributes), 0, 0, 0)
    body = bytearray()
    for ns, name, data_type, value, _res_id in attributes:
        # ResXMLTree_attribute: ns(4) name(4) rawValue(4) size(2) res0(1) dataType(1) data(4)
        body.extend(struct.pack("<IIIHBBI", ns, name, NO_ENTRY, 8, 0, data_type, value))
    size = 16 + len(ext) + len(body)
    header = struct.pack("<HHIII", AXML_START_ELEMENT_TYPE, 16, size, 1, NO_ENTRY)
    return header + ext + bytes(body)


def _end_element(name_index: int) -> bytes:
    ext = struct.pack("<II", NO_ENTRY, name_index)
    size = 16 + len(ext)
    header = struct.pack("<HHIII", 0x0103, 16, size, 1, NO_ENTRY)
    return header + ext


def build_apk(
    *,
    package_name: str = "com.example.test",
    label: str = "Тестовое приложение",
    icon_bytes: bytes = b"\x89PNG\r\n\x1a\n" + b"fake-icon-payload" * 8,
    with_manifest: bool = True,
    with_icon: bool = True,
) -> bytes:
    """Собрать zip-APK с манифестом, таблицей ресурсов и иконкой."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("resources.arsc", resource_table(label=label))
        if with_manifest:
            archive.writestr("AndroidManifest.xml", manifest_axml(package_name=package_name))
        if with_icon:
            archive.writestr("res/mipmap/ic_launcher.png", icon_bytes)
    return buffer.getvalue()
