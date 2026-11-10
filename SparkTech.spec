# -*- mode: python ; coding: utf-8 -*-
"""Сборка SparkTech в один Windows .exe (PyInstaller).

Использование (Windows):
    python -m pip install pyinstaller pywebview
    pyinstaller SparkTech.spec --noconfirm

Результат: dist/SparkTech.exe

Официальный adb (Android SDK Platform-Tools) НЕ встраивается автоматически:
включить его в сборку можно только вручную и по лицензии Google —
положите platform-tools/adb.exe (+ AdbWinApi.dll, AdbWinUsbApi.dll) в
vendor/platform-tools перед сборкой. Скрипт scripts/build_windows.ps1
делает это, если каталог существует, и предупреждает, если нет.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()  # noqa: F821 — задаётся PyInstaller
VENDOR = ROOT / "vendor" / "platform-tools"

datas = [(str(ROOT / "sparktech" / "web" / "static"), "sparktech/web/static")]
binaries = []

if VENDOR.is_dir():
    adb_candidates = list(VENDOR.glob("adb.exe")) + list(VENDOR.glob("adb"))
    if adb_candidates:
        # adb.exe и его DLL кладутся рядом с исполняемым файлом в platform-tools/
        for item in VENDOR.iterdir():
            if item.is_file():
                binaries.append((str(item), "vendor/platform-tools"))
    else:
        print("[SparkTech] ВНИМАНИЕ: vendor/platform-tools есть, но adb.exe не найден.")
else:
    print(
        "[SparkTech] vendor/platform-tools не найден: .exe соберётся без adb.\n"
        "            Пользователь укажет путь к официальным Platform-Tools в настройках."
    )

hiddenimports = []
try:
    import webview  # noqa: F401

    hiddenimports += ["webview", "webview.platforms.edgechromium", "clr_loader"]
except Exception:  # pywebview необязателен
    print("[SparkTech] pywebview не установлен: сборка без нативного окна (интерфейс в браузере).")

hiddenimports += collect_submodules("sparktech")

# PyInstaller запускает указанный скрипт как отдельный файл, поэтому
# sparktech/__main__.py (с относительными импортами) использовать нельзя —
# берём тонкую обёртку с абсолютным импортом.
a = Analysis(  # noqa: F821
    [str(ROOT / "run_sparktech.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest", "pydoc", "email", "http.server.hooks"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SparkTech",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI-приложение без чёрного окна консоли
    disable_windowed_traceback=False,
    icon=None,
    version=str(ROOT / "packaging" / "version_info.txt") if (ROOT / "packaging" / "version_info.txt").exists() else None,
)
