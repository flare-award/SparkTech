"""Чтение ресурсов APK: настоящие названия приложений и их иконки.

Зачем это нужно: ``pm list packages`` даёт только имя пакета
(``com.whatsapp``), а пользователю нужно «WhatsApp». Иконка тоже лежит в
ресурсах APK, а не в отдельном «API иконок». Официального способа получить их
через командную строку adb нет, поэтому SparkTech разбирает тот минимум
формата Android Resources (``resources.arsc``) и бинарного
``AndroidManifest.xml`` (AXML), который нужен для двух значений: ``label`` и
``icon`` из ``<application>``.

Что реализовано (по спецификации формата):

* ``ResStringPool`` — пул строк в UTF-8 и UTF-16 вариантах;
* ``ResTable`` — таблица ресурсов: пакеты, типы, конфигурации, записи;
* разрешение ``@0x7f0e0001`` (ссылка на ссылку — тоже);
* AXML: строка-пул файла, карта ресурсов (``RES_XML_RESOURCE_MAP``) и
  атрибуты элемента ``<application>``;
* чтение файла ресурса (PNG/WebP) из zip-архива APK.

Чего здесь нет: стилей, массивов, тем, сложных конфигураций и
мультипакетных APK (split). Если что-то не разобралось — возвращается «нет
данных», и интерфейс показывает имя пакета и нейтральную заглушку. Никогда не
подставляется чужое название или чужая иконка.
"""

from __future__ import annotations

import struct
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

RES_STRING_POOL_TYPE = 0x0001
RES_TABLE_TYPE = 0x0002
RES_TABLE_PACKAGE_TYPE = 0x0200
RES_TABLE_TYPE_TYPE = 0x0201

AXML_FILE_TYPE = 0x0003
AXML_STRING_POOL_TYPE = 0x0001
AXML_RESOURCE_MAP_TYPE = 0x0180
AXML_START_ELEMENT_TYPE = 0x0102

TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03

FLAG_COMPLEX = 0x0001
FLAG_SPARSE = 0x01
FLAG_OFFSET16 = 0x02

# Идентификаторы атрибутов android:label и android:icon — стабильны во всех
# версиях Android (это часть публичного API фреймворка).
ANDROID_ATTR_LABEL = 0x01010001
ANDROID_ATTR_ICON = 0x01010002
ANDROID_ATTR_NAME = 0x01010003


class ResourceTableError(ValueError):
    """Файл не похож на таблицу ресурсов Android."""


def _u8(data: bytes, offset: int) -> int:
    return data[offset]


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _len8(data: bytes, offset: int) -> tuple[int, int]:
    """Длина строки UTF-8-пула: 1 или 2 байта (старший бит — признак продолжения)."""
    first = data[offset]
    if first & 0x80:
        return ((first & 0x7F) << 8) | data[offset + 1], offset + 2
    return first, offset + 1


def _len16(data: bytes, offset: int) -> tuple[int, int]:
    """Длина строки UTF-16-пула: 2 или 4 байта."""
    first = _u16(data, offset)
    if first & 0x8000:
        return ((first & 0x7FFF) << 16) | _u16(data, offset + 2), offset + 4
    return first, offset + 2


class StringPool:
    """``ResStringPool``: массив строк, на которые ссылаются все остальные структуры."""

    def __init__(self, data: bytes, offset: int):
        if offset + 28 > len(data):
            raise ResourceTableError("Пул строк выходит за пределы файла")
        if _u16(data, offset) != RES_STRING_POOL_TYPE:
            raise ResourceTableError("Ожидался пул строк")
        self._data = data
        self._base = offset
        self.count = _u32(data, offset + 8)
        self.flags = _u32(data, offset + 16)
        self.strings_start = _u32(data, offset + 20)
        self.utf8 = bool(self.flags & 0x100)
        header_size = _u16(data, offset + 2)
        self._offsets = [
            _u32(data, offset + header_size + index * 4) for index in range(self.count)
        ]

    def get(self, index: int) -> str:
        if index < 0 or index >= self.count:
            return ""
        position = self._base + self.strings_start + self._offsets[index]
        try:
            if self.utf8:
                _chars, position = _len8(self._data, position)
                byte_len, position = _len8(self._data, position)
                return self._data[position:position + byte_len].decode("utf-8", "replace")
            char_len, position = _len16(self._data, position)
            return self._data[position:position + char_len * 2].decode("utf-16-le", "replace")
        except (IndexError, struct.error):
            return ""


@dataclass
class ResValue:
    """Разобранное ``Res_value``: тип и сырые данные."""

    data_type: int = 0
    data: int = 0

    @property
    def is_reference(self) -> bool:
        return self.data_type == TYPE_REFERENCE

    @property
    def is_string(self) -> bool:
        return self.data_type == TYPE_STRING


@dataclass
class TypeChunk:
    """Один блок типа с конкретной конфигурацией устройства."""

    offset: int
    type_id: int
    flags: int
    entry_count: int
    entries_start: int
    config: bytes
    header_size: int


@dataclass
class PackageTable:
    package_id: int
    type_strings: StringPool | None = None
    key_strings: StringPool | None = None
    chunks: list[TypeChunk] = field(default_factory=list)


class ResourceTable:
    """Таблица ``resources.arsc`` — то, что внутри APK называется ресурсами."""

    def __init__(self, data: bytes):
        if len(data) < 12 or _u16(data, 0) != RES_TABLE_TYPE:
            raise ResourceTableError("Файл не является таблицей ресурсов Android")
        self.data = data
        header_size = _u16(data, 2)
        total_size = _u32(data, 4)
        self.global_strings = StringPool(data, header_size)
        self.packages: dict[int, PackageTable] = {}
        # Пакеты идут сразу после глобального пула строк: его размер лежит в
        # заголовке пула, а не в заголовке таблицы (частая ошибка).
        end = min(total_size, len(data)) if total_size else len(data)
        packages_offset = header_size
        if _u16(data, header_size) == RES_STRING_POOL_TYPE:
            pool_size = _u32(data, header_size + 4)
            if pool_size >= 8:
                packages_offset = header_size + pool_size
        self._parse_packages(packages_offset, end)

    # --------------------------------------------------------------- разбор
    def _parse_packages(self, offset: int, end: int) -> None:
        data = self.data
        while offset + 8 <= end:
            chunk_type = _u16(data, offset)
            header_size = _u16(data, offset + 2)
            size = _u32(data, offset + 4)
            # Последний блок заканчивается ровно на конце таблицы, поэтому
            # строгое «больше конца» — ошибка, а не проверка.
            if size < 8 or offset + size > end:
                break
            if chunk_type == RES_TABLE_PACKAGE_TYPE:
                package_id = _u32(data, offset + 8)
                # Смещения пулов строк пакета отсчитываются от начала блока пакета.
                type_strings_offset = _u32(data, offset + 268)
                key_strings_offset = _u32(data, offset + 276)
                table = PackageTable(package_id=package_id)
                table.type_strings = self._safe_pool(offset + type_strings_offset)
                table.key_strings = self._safe_pool(offset + key_strings_offset)
                table.chunks = self._collect_type_chunks(offset + header_size, min(offset + size, len(data)))
                self.packages[package_id] = table
            offset += size

    def _safe_pool(self, offset: int) -> StringPool | None:
        try:
            return StringPool(self.data, offset)
        except (ResourceTableError, struct.error, IndexError):
            return None

    def _collect_type_chunks(self, offset: int, end: int) -> list[TypeChunk]:
        data = self.data
        chunks: list[TypeChunk] = []
        while offset + 8 <= end:
            chunk_type = _u16(data, offset)
            header_size = _u16(data, offset + 2)
            size = _u32(data, offset + 4)
            if size < 8:
                break
            if chunk_type == RES_TABLE_TYPE_TYPE:
                chunks.append(
                    TypeChunk(
                        offset=offset,
                        type_id=_u8(data, offset + 8),
                        flags=_u8(data, offset + 9),
                        entry_count=_u32(data, offset + 12),
                        entries_start=_u32(data, offset + 16),
                        config=data[offset + 20:offset + header_size],
                        header_size=header_size,
                    )
                )
            offset += size
        return chunks

    # -------------------------------------------------------------- значения
    def value_of(self, res_id: int, depth: int = 0) -> ResValue | None:
        """Найти значение ресурса по ``0xPPTTEEEE``.

        Предпочитается конфигурация «по умолчанию» (без языка/плотности):
        она есть почти всегда и не зависит от телефона.
        """
        if depth > 8:
            return None
        package_id = (res_id >> 24) & 0xFF
        type_id = (res_id >> 16) & 0xFF
        index = res_id & 0xFFFF
        package = self.packages.get(package_id)
        if package is None:
            return None

        best: tuple[int, ResValue] | None = None
        for chunk in package.chunks:
            if chunk.type_id != type_id:
                continue
            entry_offset = self._entry_offset(chunk, index)
            if entry_offset is None:
                continue
            value = self._read_entry_value(chunk, entry_offset)
            if value is None:
                continue
            default = self._is_default_config(chunk.config)
            score = 2 if default else 1
            if best is None or score > best[0]:
                best = (score, value)
            if default:
                break
        if best is None:
            return None
        value = best[1]
        if value.is_reference:
            # @drawable/ic_launcher может ссылаться на другой ресурс.
            return self.value_of(value.data, depth + 1)
        return value

    def string_of(self, res_id: int) -> str:
        value = self.value_of(res_id)
        if value is None or not value.is_string:
            return ""
        return self.global_strings.get(value.data).strip()

    def file_of(self, res_id: int) -> str:
        """Путь файла ресурса внутри APK (``res/mipmap.../ic_launcher.png``)."""
        value = self.value_of(res_id)
        if value is None or not value.is_string:
            return ""
        return self.global_strings.get(value.data).strip()

    @staticmethod
    def _is_default_config(config: bytes) -> bool:
        return not any(config[4:]) if len(config) > 4 else True

    def _entry_offset(self, chunk: TypeChunk, index: int) -> int | None:
        data = self.data
        base = chunk.offset + chunk.header_size
        if index >= chunk.entry_count:
            return None
        if chunk.flags & FLAG_SPARSE:
            for position in range(chunk.entry_count):
                pos = base + position * 4
                if _u16(data, pos) == index:
                    return _u16(data, pos + 2) * 4
            return None
        if chunk.flags & FLAG_OFFSET16:
            raw = _u16(data, base + index * 2)
            return None if raw == 0xFFFF else raw * 4
        raw = _u32(data, base + index * 4)
        if raw == 0xFFFFFFFF:
            return None
        return raw & 0x00FFFFFF

    def _read_entry_value(self, chunk: TypeChunk, entry_offset: int) -> ResValue | None:
        data = self.data
        position = chunk.offset + chunk.entries_start + entry_offset
        if position + 8 > len(data):
            return None
        flags = _u16(data, position + 2)
        if flags & FLAG_COMPLEX:
            return None  # стили/массивы нам не нужны
        value_position = position + _u16(data, position)  # = position + size записи
        if value_position + 8 > len(data):
            return None
        return ResValue(data_type=_u8(data, value_position + 3), data=_u32(data, value_position + 4))


# --------------------------------------------------------------------- AXML


@dataclass
class ManifestInfo:
    """Атрибуты ``<application>`` из бинарного ``AndroidManifest.xml``."""

    label_res_id: int = 0
    icon_res_id: int = 0
    label_text: str = ""
    package_name: str = ""

    @property
    def has_data(self) -> bool:
        return bool(self.label_res_id or self.icon_res_id or self.label_text)


def parse_manifest(data: bytes) -> ManifestInfo:
    """Прочитать AXML и достать ``android:label``/``android:icon`` у ``<application>``.

    Формат: заголовок файла, пул строк, карта ресурсов (массив ID по индексам
    строк), затем поток элементов. У атрибутов ``name`` — индекс в пуле строк,
    а ресурсный идентификатор берётся из карты: именно так ``android:label``
    отличается от произвольного атрибута с тем же текстом.
    """
    if len(data) < 8 or _u16(data, 0) != AXML_FILE_TYPE:
        raise ResourceTableError("AndroidManifest.xml не в бинарном формате AXML")

    info = ManifestInfo()
    strings: StringPool | None = None
    resource_map: list[int] = []

    offset = _u16(data, 2)
    total = _u32(data, 4) or len(data)
    end = min(total, len(data))
    while offset + 8 <= end:
        chunk_type = _u16(data, offset)
        header_size = _u16(data, offset + 2)
        size = _u32(data, offset + 4)
        if size < 8:
            break
        if chunk_type == AXML_STRING_POOL_TYPE and strings is None:
            strings = StringPool(data, offset)
        elif chunk_type == AXML_RESOURCE_MAP_TYPE:
            count = (size - header_size) // 4
            resource_map = [_u32(data, offset + header_size + i * 4) for i in range(count)]
        elif chunk_type == AXML_START_ELEMENT_TYPE:
            found = _read_element(data, offset, header_size, strings, resource_map)
            if found is not None:
                # Корневой <manifest> даёт имя пакета, <application> — label/icon.
                # Важно не прерываться на первом элементе: <manifest> идёт первым.
                if found.package_name and not info.package_name:
                    info.package_name = found.package_name
                info.label_res_id = info.label_res_id or found.label_res_id
                info.icon_res_id = info.icon_res_id or found.icon_res_id
                info.label_text = info.label_text or found.label_text
                if info.label_res_id or info.icon_res_id or info.label_text:
                    break
        offset += size
    return info


def _read_element(
    data: bytes,
    offset: int,
    header_size: int,
    strings: StringPool | None,
    resource_map: list[int],
) -> ManifestInfo | None:
    """Разобрать один ``START_ELEMENT``: имя элемента и его атрибуты."""
    info = ManifestInfo()
    if strings is None:
        return None
    # Заголовок элемента: lineNumber(4) + comment(4), затем:
    # ns(4) name(4) attributeStart(2) attributeSize(2) attributeCount(2) ...
    ext = offset + 16
    element_name = strings.get(_u32(data, ext + 4))
    attribute_start = _u16(data, ext + 8)
    attribute_size = _u16(data, ext + 10) or 20
    attribute_count = _u16(data, ext + 12)

    # attributeStart отсчитывается от начала extension (сразу после node header
    # в 16 байт), а не от начала всего заголовка блока.
    attributes_base = offset + 16

    if element_name == "manifest":
        for index in range(attribute_count):
            pos = attributes_base + attribute_start + index * attribute_size
            if pos + 20 > len(data):
                break
            name_index = _u32(data, pos + 4)
            if strings.get(name_index) == "package":
                info.package_name = _typed_string(data, pos, strings)
        return info

    if element_name != "application":
        return None

    for index in range(attribute_count):
        pos = attributes_base + attribute_start + index * attribute_size
        if pos + 20 > len(data):
            break
        name_index = _u32(data, pos + 4)
        res_id = resource_map[name_index] if name_index < len(resource_map) else 0
        data_type = _u8(data, pos + 15)
        value = _u32(data, pos + 16)
        if res_id == ANDROID_ATTR_LABEL:
            if data_type == TYPE_REFERENCE:
                info.label_res_id = value
            elif data_type == TYPE_STRING:
                info.label_text = strings.get(value).strip()
        elif res_id == ANDROID_ATTR_ICON:
            if data_type == TYPE_REFERENCE:
                info.icon_res_id = value
    return info


def _typed_string(data: bytes, pos: int, strings: StringPool) -> str:
    data_type = _u8(data, pos + 15)
    value = _u32(data, pos + 16)
    if data_type == TYPE_STRING:
        return strings.get(value).strip()
    return ""


# ---------------------------------------------------------------- APK-доступ


@dataclass
class ApkResources:
    """Ресурсы APK, прочитанного с устройства (начало файла)."""

    resources: ResourceTable
    manifest: ManifestInfo
    zip_data: bytes

    @classmethod
    def from_apk_intro(cls, data: bytes) -> "ApkResources":
        """Открыть APK из прочитанных первых байт.

        Zip-архив читается по локальным заголовкам, поэтому начала файла
        достаточно для ``resources.arsc`` и небольшой PNG-иконки. Если файл
        обрезан так, что нужного файла нет, возвращается понятная ошибка.
        """
        if not data.startswith(b"PK\x03\x04"):
            raise ResourceTableError("Файл не похож на APK (zip)")
        try:
            archive = zipfile.ZipFile(BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ResourceTableError(f"APK прочитан не полностью: {exc}") from exc
        with archive:
            names = set(archive.namelist())
            if "resources.arsc" not in names:
                raise ResourceTableError("В APK нет resources.arsc")
            resources = ResourceTable(archive.read("resources.arsc"))
            manifest = ManifestInfo()
            if "AndroidManifest.xml" in names:
                try:
                    manifest = parse_manifest(archive.read("AndroidManifest.xml"))
                except ResourceTableError:
                    manifest = ManifestInfo()
        return cls(resources=resources, manifest=manifest, zip_data=data)

    def application_label(self) -> str:
        """Название приложения: из ``android:label`` (ресурс или строка)."""
        if self.manifest.label_text:
            return self.manifest.label_text
        if self.manifest.label_res_id:
            return self.resources.string_of(self.manifest.label_res_id)
        return ""

    def icon_file(self) -> str:
        """Путь файла иконки внутри APK (например, ``res/mipmap/ic_launcher.png``)."""
        if not self.manifest.icon_res_id:
            return ""
        return self.resources.file_of(self.manifest.icon_res_id)

    def read_file(self, name: str) -> bytes:
        if not name:
            return b""
        with zipfile.ZipFile(BytesIO(self.zip_data)) as archive:
            try:
                return archive.read(name)
            except KeyError:
                return b""

    def icon_bytes(self) -> bytes:
        """Содержимое иконки (PNG/WebP) или пустые байты."""
        return self.read_file(self.icon_file())


__all__ = [
    "ANDROID_ATTR_ICON",
    "ANDROID_ATTR_LABEL",
    "ApkResources",
    "ManifestInfo",
    "PackageTable",
    "ResValue",
    "ResourceTable",
    "ResourceTableError",
    "StringPool",
    "parse_manifest",
]
