#!/usr/bin/env python3
"""Макет adb для автотестов и ручной проверки без телефона.

Имитирует CLI официального adb настолько, насколько нужно SparkTech, и
выполняет `shell`-команды **настоящей** POSIX-оболочкой по «файловой системе
телефона» в каталоге ``$SPARKTECH_MOCK_ROOT`` (пути вида
``/storage/emulated/0/...`` перенаправляются туда).

Это позволяет проверить реальный конвейер: экранирование путей, `find`,
разбор вывода, `rm`, проверку результата — без устройства. Не является
эмуляцией Android: настройки, df и pidof подделываются явно.

Запуск SparkTech с макетом:

    SPARKTECH_MOCK_ROOT=/tmp/mockphone python scripts/mock_adb.py ...
    python -m sparktech --adb scripts/mock_adb.py
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.environ.get("SPARKTECH_MOCK_ROOT", "/tmp/mockphone")
SERIAL = os.environ.get("SPARKTECH_MOCK_SERIAL", "MOCKDEVICE01")
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mock_settings")

MAP_PREFIX = "/storage/emulated/0"


# Каталоги установки приложений тоже живут «в телефоне»: в макете они
# отображаются внутрь $SPARKTECH_MOCK_ROOT, чтобы метаданные приложений
# (размер APK, иконка) читались так же, как на настоящем устройстве.
EXTRA_MOUNTS = ("/data/app", "/system/priv-app", "/system/app", "/product/app")


def map_path(path: str) -> str:
    if path == MAP_PREFIX:
        return ROOT
    if path.startswith(MAP_PREFIX + "/"):
        return ROOT + path[len(MAP_PREFIX):]
    if path.startswith("/sdcard"):
        return map_path(MAP_PREFIX + path[len("/sdcard"):])
    for mount in EXTRA_MOUNTS:
        if path == mount or path.startswith(mount + "/"):
            return ROOT + path
    return path


def rewrite(command: str) -> str:
    """Подменить пути телефона на каталог макета.

    Это тестовая среда, поэтому простая замена строк допустима: настоящий
    SparkTech всегда передаёт пути отдельными аргументами с экранированием.
    """
    text = command.replace(MAP_PREFIX, ROOT).replace("'/sdcard", "'" + ROOT)
    for mount in EXTRA_MOUNTS:
        text = text.replace("'" + mount, "'" + ROOT + mount)
    return text


def read_settings() -> dict[str, str]:
    if not os.path.exists(SETTINGS_FILE):
        return {}
    data = {}
    with open(SETTINGS_FILE, encoding="utf-8") as fh:
        for line in fh:
            if "=" in line:
                k, _, v = line.rstrip("\n").partition("=")
                data[k] = v
    return data


def write_settings(data: dict[str, str]) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        for k, v in data.items():
            fh.write(f"{k}={v}\n")


# Пакеты макета: имя, путь APK, версия, системный, даты установки/обновления.
# Размеры и иконки берутся из настоящих APK в макете файловой системы, поэтому
# экран приложений проверяется целиком, без телефона.
MOCK_PACKAGES = [
    ("com.example.chat", "2.24.3.79", False, "2022-11-03 18:22:41", "2024-05-17 09:07:12", "2024-06-01 20:15:03"),
    ("com.example.player", "5.1.0", False, "2023-02-11 10:00:00", "2023-02-11 10:00:00", "2024-03-14 08:11:00"),
    ("com.android.settings", "13", True, "2022-01-01 00:00:00", "2022-01-01 00:00:00", "2024-06-10 12:00:00"),
]


def mock_dumpsys_packages() -> str:
    lines = ["Packages:"]
    for index, (pkg, version, system, installed, updated, used) in enumerate(MOCK_PACKAGES, start=1):
        lines.append(f"  Package [{pkg}] (mock{index}):")
        lines.append(f"    userId={10100 + index}")
        lines.append(f"    codePath={apk_path_of(pkg)}")
        lines.append(f"    versionName={version}")
        lines.append(f"    firstInstallTime={installed}")
        lines.append(f"    lastUpdateTime={updated}")
        lines.append(f"    lastTimeUsed={used}")
        lines.append("    pkgFlags=[ HAS_CODE ALLOW_CLEAR_USER_DATA" + (" SYSTEM" if system else "") + " ]")
        lines.append("    enabled=true")
    return "\n".join(lines) + "\n"


def apk_path_of(package: str) -> str:
    if package.startswith("com.android"):
        return f"/system/priv-app/{package.split('.')[-1].title()}/base.apk"
    return f"/data/app/~~mock==/{package}-1/base.apk"


def mock_package_list() -> str:
    return "".join(f"package:{apk_path_of(pkg) if not pkg.startswith('com.android') else apk_path_of(pkg).replace('/base.apk', '/X.apk')}={pkg}\n" for pkg, *_ in MOCK_PACKAGES)


def mock_diskstats() -> str:
    import base64, struct, zlib

    names = [pkg for pkg, *_ in MOCK_PACKAGES]
    data = [320_000_000, 140_000_000, 8_000_000]
    cache = [42_000_000, 6_000_000, 0]
    pack = lambda values: base64.b64encode(zlib.compress(struct.pack(f"<{len(values)}q", *values))).decode()
    return (
        f"packageSizes: {','.join(names)}\n"
        f"packageDataSizes: {pack(data)}\n"
        f"cacheSizes: {pack(cache)}\n"
    )


def handle_shell(command: str) -> int:
    cmd = command.strip()

    if cmd == "dumpsys package packages":
        sys.stdout.write(mock_dumpsys_packages())
        return 0
    if cmd.startswith("pm list packages"):
        sys.stdout.write(mock_package_list())
        return 0
    if cmd == "dumpsys diskstats":
        sys.stdout.write(mock_diskstats())
        return 0

    if cmd.startswith("getprop"):
        key = cmd[len("getprop"):].strip()
        props = {
            "ro.product.model": "Mock Pixel 7",
            "ro.product.manufacturer": "Google",
            "ro.build.version.release": "13",
            "ro.build.version.sdk": "33",
            "ro.product.cpu.abi": "arm64-v8a",
        }
        if key:
            print(props.get(key, ""))
        else:
            for k, v in props.items():
                print(f"[{k}]: [{v}]")
        return 0

    if cmd.startswith("df -k"):
        print("Filesystem     1K-blocks     Used Available Use% Mounted on")
        print("/dev/fuse      122070312 50000000  72070312  41% /storage/emulated/0")
        return 0

    if cmd.startswith("settings get global"):
        key = cmd.split()[-1]
        print(read_settings().get(key, "null"))
        return 0

    if cmd.startswith("settings put global"):
        parts = cmd.split()
        key, value = parts[3], parts[4].strip("'")
        data = read_settings()
        data[key] = value
        write_settings(data)
        return 0

    if cmd.startswith("pidof"):
        target = cmd.split()[-1]
        print("1234" if target else "")
        return 0

    if "pkill" in cmd or "killall" in cmd:
        return 0

    if cmd.startswith("content call"):
        print("Broadcast completed: result=0")
        return 0

    if cmd.startswith("cmd "):
        print("ok")
        return 0

    if "dumpsys battery" in cmd:
        print("Current Battery Service state:\n  level: 76\n  status: 2")
        return 0

    # Всё остальное — настоящая оболочка на макете файловой системы.
    # Вывод переводим обратно в «телефонные» пути, как у настоящего adb.
    proc = subprocess.run(["sh", "-c", rewrite(cmd)], capture_output=True, text=True)
    # Обратная замена: пути макета -> пути «телефона». Важен порядок, иначе
    # /data/app превратился бы в /storage/emulated/0/data/app.
    out = proc.stdout.replace(ROOT + MAP_PREFIX, MAP_PREFIX)
    for mount in EXTRA_MOUNTS:
        out = out.replace(ROOT + mount, mount)
    out = out.replace(ROOT, MAP_PREFIX)
    err = proc.stderr.replace(ROOT, MAP_PREFIX)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return proc.returncode


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("version", "--version"):
        print("Android Debug Bridge version 1.0.41")
        print("Version 35.0.2-mock")
        return 0
    if argv[0] in ("start-server", "kill-server"):
        return 0
    if argv[0] == "devices":
        print("List of devices attached")
        print(f"{SERIAL}\tdevice product:mock model:Mock_Pixel_7 device:mock transport_id:1")
        return 0

    # -s SERIAL ...
    if argv[0] == "-s":
        rest = argv[2:]
    else:
        rest = argv
    if rest and rest[0] == "get-state":
        print("device")
        return 0
    if rest and rest[0] == "shell":
        command = " ".join(rest[1:])
        return handle_shell(command)
    if rest and rest[0] == "pull":
        # Скопировать файл из макета «телефона» на компьютер, как настоящий adb.
        if len(rest) < 3:
            print("adb: pull требует источник и назначение", file=sys.stderr)
            return 1
        source = map_path(rest[1])
        target = rest[2]
        if not os.path.isfile(source):
            print(f"adb: error: failed to stat remote object '{rest[1]}'", file=sys.stderr)
            return 1
        import shutil
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        shutil.copyfile(source, target)
        print(f"{rest[1]}: 1 file pulled.")
        return 0
    if rest and rest[0] == "exec-out":
        command = " ".join(rest[1:])
        if command.startswith("cat ") or command.startswith("head "):
            proc = subprocess.run(["sh", "-c", rewrite(command)], capture_output=True)
            sys.stdout.buffer.write(proc.stdout)
            sys.stderr.buffer.write(proc.stderr)
            return proc.returncode
        return 0
    print(f"mock adb: неизвестная команда: {rest}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
