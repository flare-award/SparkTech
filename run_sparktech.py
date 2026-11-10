#!/usr/bin/env python3
"""Точка входа для сборки PyInstaller и для запуска из клона без установки.

PyInstaller указывает на скрипт как на отдельный файл, а не как на модуль
пакета: при запуске ``sparktech/__main__.py`` напрямую относительный импорт
``from . import __version__`` падает с ImportError. Этот файл делает импорт
абсолютным и потому работает в обоих случаях.

Запуск из исходников:      python run_sparktech.py
Сборка исполняемого файла: python -m PyInstaller SparkTech.spec
"""

from __future__ import annotations

import sys
from pathlib import Path

# Кодировка консоли (см. _safe_console ниже) настраивается до импорта пакета,
# чтобы ни одно сообщение при старте не упало с UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Если запускаем из клона, каталог репозитория должен быть в sys.path.
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sparktech.__main__ import main  # noqa: E402  (после настройки sys.path)

if __name__ == "__main__":
    raise SystemExit(main())
