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


def map_path(path: str) -> str:
    if path == MAP_PREFIX:
        return ROOT
    if path.startswith(MAP_PREFIX + "/"):
        return ROOT + path[len(MAP_PREFIX):]
    if path.startswith("/sdcard"):
        return map_path(MAP_PREFIX + path[len("/sdcard"):])
    return path


def rewrite(command: str) -> str:
    """Подменить пути телефона на каталог макета (вне кавычек не нужно —
    подстановка безопасна, т.к. это тестовая среда)."""
    return command.replace(MAP_PREFIX, ROOT).replace("'/sdcard", "'" + ROOT)


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


def handle_shell(command: str) -> int:
    cmd = command.strip()

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
    out = proc.stdout.replace(ROOT, MAP_PREFIX)
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
