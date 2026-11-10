"""Тесты слоя приложений: разбор `dumpsys`/`pm`/`diskstats` и метаданные.

Проверяется именно то, что видит пользователь: названия, даты, размеры,
категории. Запуск настоящего adb заменён подставным транспортом — но разбор
вывода настоящий, на образцах реальных прошивок.
"""

from __future__ import annotations

import base64
import struct
import zlib

import pytest

from sparktech.core.apps import (
    AppInfo,
    AppService,
    apk_path_of,
    category_for,
    parse_date,
    parse_diskstats,
    parse_dumpsys_packages,
    parse_pm_list_packages,
)

DUMPSYS_SAMPLE = """\
Packages:
  Package [com.whatsapp] (1a2b3c):
    userId=10150
    codePath=/data/app/~~8Hf2==/com.whatsapp-1
    resourcePath=/data/app/~~8Hf2==/com.whatsapp-1
    versionName=2.24.3.79
    firstInstallTime=2022-11-03 18:22:41
    lastUpdateTime=2024-05-17 09:07:12
    lastTimeUsed=2024-06-01 20:15:03
    pkgFlags=[ HAS_CODE ALLOW_CLEAR_USER_DATA ALLOW_BACKUP ]
    enabled=true
  Package [com.android.settings] (9f8e7d):
    userId=1000
    codePath=/system/priv-app/Settings
    versionName=13
    firstInstallTime=2022-01-01 00:00:00
    pkgFlags=[ SYSTEM HAS_CODE ALLOW_CLEAR_USER_DATA ]
    enabled=true
"""


def test_parse_dumpsys_packages_extracts_dates_and_version():
    parsed = parse_dumpsys_packages(DUMPSYS_SAMPLE)
    assert set(parsed) == {"com.whatsapp", "com.android.settings"}
    whatsapp = parsed["com.whatsapp"]
    assert whatsapp["versionName"] == "2.24.3.79"
    assert whatsapp["firstInstallTime"] == "2022-11-03 18:22:41"
    assert whatsapp["lastUpdateTime"] == "2024-05-17 09:07:12"
    assert whatsapp["lastTimeUsed"] == "2024-06-01 20:15:03"
    assert whatsapp["codePath"] == "/data/app/~~8Hf2==/com.whatsapp-1"
    assert whatsapp["uid"] == "10150"


def test_parse_dumpsys_flags_system_detection():
    parsed = parse_dumpsys_packages(DUMPSYS_SAMPLE)
    assert "SYSTEM" in parsed["com.android.settings"]["pkgFlags"]
    assert "SYSTEM" not in parsed["com.whatsapp"]["pkgFlags"]


def test_parse_dumpsys_garbage_is_ignored():
    assert parse_dumpsys_packages("совсем не тот вывод\nни одной строки Package") == {}
    assert parse_dumpsys_packages("") == {}


def test_parse_date_returns_milliseconds():
    assert parse_date("2024-05-17 09:07:12") == 1715936832000
    assert parse_date("") == 0
    assert parse_date("не дата") == 0


def test_apk_path_of_turns_code_path_into_base_apk():
    assert apk_path_of("/data/app/~~a==/com.y-1", "com.y") == "/data/app/~~a==/com.y-1/base.apk"
    assert apk_path_of("/system/app/X/X.apk", "x") == "/system/app/X/X.apk"
    assert apk_path_of("", "x") == ""


def test_parse_pm_list_packages_keeps_package_and_system_flag():
    text = (
        "package:/system/priv-app/Settings/Settings.apk=com.android.settings\n"
        "package:/data/app/~~a==/com.y-1/base.apk=com.y\n"
        "package:/product/app/Chrome/Chrome.apk=com.android.chrome\n"
    )
    parsed = parse_pm_list_packages(text)
    assert parsed == [
        ("/system/priv-app/Settings/Settings.apk", "com.android.settings", True),
        ("/data/app/~~a==/com.y-1/base.apk", "com.y", False),
        ("/product/app/Chrome/Chrome.apk", "com.android.chrome", True),
    ]


def _diskstats(packages: list[str], data_sizes: list[int], cache_sizes: list[int]) -> str:
    compressed = zlib.compress(struct.pack(f"<{len(data_sizes)}q", *data_sizes))
    compressed_cache = zlib.compress(struct.pack(f"<{len(cache_sizes)}q", *cache_sizes))
    return (
        f"packageSizes: {','.join(packages)}\n"
        f"packageDataSizes: {base64.b64encode(compressed).decode()}\n"
        f"cacheSizes: {base64.b64encode(compressed_cache).decode()}\n"
    )


def test_parse_diskstats_decodes_sizes():
    text = _diskstats(["com.a", "com.b"], [1024, 2048], [100, 200])
    parsed = parse_diskstats(text)
    assert parsed["com.a"] == {"data": 1024, "cache": 100}
    assert parsed["com.b"] == {"data": 2048, "cache": 200}


def test_parse_diskstats_broken_payload_returns_empty():
    """Неверный формат не должен выдавать «нули, похожие на правду»."""
    assert parse_diskstats("packageSizes: com.a\npackageDataSizes: мусор") == {}
    assert parse_diskstats("") == {}
    assert parse_diskstats("packageSizes: com.a,com.b\npackageDataSizes: " + _b64(b"short")) == {}


def _b64(raw: bytes) -> str:
    return base64.b64encode(zlib.compress(raw)).decode()


def test_category_for_known_and_system_apps():
    assert category_for("com.whatsapp", is_system=False) == ("messenger", "Мессенджер")
    assert category_for("com.android.systemui", is_system=True) == ("system", "Системное")
    assert category_for("com.unknown.game", is_system=False) == ("app", "Приложение")


def test_total_size_sums_apk_data_and_cache():
    app = AppInfo(package="com.a", apk_size=100, data_size=200, cache_size=50)
    assert app.total_size == 350


def test_to_dict_reports_when_label_is_unknown():
    app = AppInfo(package="com.secret.pkg")
    payload = app.to_dict()
    assert payload["label"] == "com.secret.pkg"
    assert payload["label_known"] is False


class FakeAppsAdb:
    """Минимальный транспорт: отдаёт заранее заданные выводы команд."""

    def __init__(self, dumpsys: str, pm_list: str, diskstats: str, apk: bytes = b""):
        self.dumpsys = dumpsys
        self.pm_list = pm_list
        self.diskstats = diskstats
        self.apk = apk
        self.commands: list[str] = []

    def shell(self, serial, command, **_kwargs):
        from sparktech.core.process import CommandResult

        self.commands.append(command)
        if command.startswith("dumpsys package"):
            return CommandResult([command], 0, self.dumpsys, "")
        if command.startswith("pm list packages"):
            return CommandResult([command], 0, self.pm_list, "")
        if command.startswith("dumpsys diskstats"):
            return CommandResult([command], 0, self.diskstats, "")
        if command.startswith("stat -c"):
            # Формат: `stat -c '%s|%n'` — размер и путь через вертикальную черту.
            return CommandResult(
                [command], 0,
                "500000|/data/app/~~8Hf2==/com.whatsapp-1/base.apk", "",
            )
        return CommandResult([command], 0, "", "")

    def run_binary(self, args, **_kwargs):
        return 0, self.apk, ""


def test_list_apps_merges_sources_and_reads_real_label():
    from apk_builder import build_apk

    adb = FakeAppsAdb(
        dumpsys=DUMPSYS_SAMPLE,
        pm_list=(
            "package:/data/app/~~a==/com.whatsapp-1/base.apk=com.whatsapp\n"
            "package:/system/priv-app/Settings/Settings.apk=com.android.settings\n"
        ),
        diskstats=_diskstats(["com.whatsapp", "com.android.settings"], [3000, 100], [1000, 0]),
        apk=build_apk(label="WhatsApp"),
    )
    apps = AppService(adb).list_apps("SERIAL", with_resources=True)
    by_pkg = {a.package: a for a in apps}

    whatsapp = by_pkg["com.whatsapp"]
    assert whatsapp.label == "WhatsApp"
    # Путь берётся из dumpsys (реальный каталог установки), а pm list лишь
    # дополняет список теми пакетами, которых в dumpsys не было.
    assert whatsapp.apk_path == "/data/app/~~8Hf2==/com.whatsapp-1/base.apk"
    assert whatsapp.apk_size == 500000
    # diskstats даёт «данные + кэш», в интерфейсе они разделены.
    assert whatsapp.data_size == 2000
    assert whatsapp.cache_size == 1000
    assert whatsapp.installed_at > 0 and whatsapp.updated_at > 0 and whatsapp.last_used_at > 0
    assert whatsapp.category_label == "Мессенджер"
    assert not whatsapp.is_system

    settings = by_pkg["com.android.settings"]
    assert settings.is_system
    assert settings.critical


def test_list_apps_icon_bytes_from_apk():
    from apk_builder import build_apk

    adb = FakeAppsAdb(
        dumpsys=DUMPSYS_SAMPLE,
        pm_list="package:/data/app/~~a==/com.whatsapp-1/base.apk=com.whatsapp\n",
        diskstats=_diskstats(["com.whatsapp"], [0], [0]),
        apk=build_apk(label="WhatsApp"),
    )
    service = AppService(adb)
    apps = service.list_apps("SERIAL", with_resources=False)
    icon = service.icon("SERIAL", apps[0])
    assert icon and icon.startswith(b"\x89PNG")


def test_list_apps_without_apk_does_not_fail():
    adb = FakeAppsAdb(dumpsys=DUMPSYS_SAMPLE, pm_list="", diskstats="", apk=b"")
    apps = AppService(adb).list_apps("SERIAL", with_resources=True)
    assert {a.package for a in apps} == {"com.whatsapp", "com.android.settings"}
    assert all(not a.icon_available for a in apps)


def test_broken_icon_never_raises():
    adb = FakeAppsAdb(
        dumpsys=DUMPSYS_SAMPLE,
        pm_list="package:/data/app/~~a==/com.whatsapp-1/base.apk=com.whatsapp\n",
        diskstats="",
        apk=b"not an apk",
    )
    service = AppService(adb)
    apps = service.list_apps("SERIAL", with_resources=False)
    assert service.icon("SERIAL", apps[0]) is None


def test_missing_dates_are_flagged_as_notes():
    adb = FakeAppsAdb(
        dumpsys="  Package [com.x] (1):\n    userId=1\n",
        pm_list="",
        diskstats="",
    )
    apps = AppService(adb).list_apps("SERIAL", with_resources=False)
    assert apps[0].installed_at == 0
    assert any("Дата установки" in note for note in apps[0].notes)


def test_sorting_puts_user_apps_first_by_size():
    adb = FakeAppsAdb(
        dumpsys=(
            "  Package [com.small] (1):\n    userId=1\n    pkgFlags=[ HAS_CODE ]\n"
            "  Package [com.android.big] (2):\n    userId=2\n    pkgFlags=[ SYSTEM HAS_CODE ]\n"
        ),
        pm_list="",
        diskstats=_diskstats(["com.small", "com.android.big"], [10, 9999], [0, 0]),
        apk=b"",
    )
    apps = AppService(adb).list_apps("SERIAL", with_resources=False)
    assert [a.package for a in apps] == ["com.small", "com.android.big"]


@pytest.mark.parametrize("path", ["", "/", "/data/app"])
def test_weird_code_path_does_not_crash(path):
    assert isinstance(apk_path_of(path, "com.x"), str)
