"""Локальное хранилище настроек, журнала и сохранённых значений.

Приватность:

* настройки и журнал лежат в профиле пользователя
  (``%LOCALAPPDATA%\\SparkTech`` на Windows, ``~/.local/state/sparktech``
  на других системах);
* журнал не содержит паролей, содержимого файлов и полных путей к
  пользовательским файлам — только тип операции, счётчики и код результата;
* содержимое файлов телефона (или их хеши с привязкой к пути) не
  сохраняются на диск.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path


def app_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "SparkTech"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "sparktech"
    return Path.home() / ".local" / "state" / "sparktech"


class Store:
    def __init__(self, data_dir: Path | str | None = None):
        self.dir = Path(data_dir) if data_dir else app_data_dir()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.dir = Path.home() / ".sparktech"
            self.dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.dir / "settings.json"
        self.log_file = self.dir / "operations.log"
        self._lock = threading.Lock()

    # ------------------------------------------------------------- настройки

    def load_settings(self) -> dict:
        try:
            with open(self.settings_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self, data: dict) -> None:
        with self._lock:
            tmp = self.settings_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.settings_file)

    def update_settings(self, patch: dict) -> dict:
        data = self.load_settings()
        allowed = {
            "adb_path", "big_file_threshold", "min_size", "include_apk", "include_archives",
            "include_temp", "include_downloads", "find_duplicates", "full_hash", "last_serial",
        }
        for key, value in (patch or {}).items():
            if key in allowed and value is not None:
                data[key] = value
        self.save_settings(data)
        return data

    # ------------------------------------------- сохранённые значения настроек

    def _settings_origin(self) -> dict:
        data = self.load_settings()
        origin = data.get("setting_origins")
        return origin if isinstance(origin, dict) else {}

    def save_setting_origin(self, serial: str, key: str, value: str) -> None:
        data = self.load_settings()
        origin = data.setdefault("setting_origins", {})
        per_device = origin.setdefault(serial, {})
        if key not in per_device:
            per_device[key] = value
        self.save_settings(data)

    def get_saved_setting(self, serial: str, key: str) -> str | None:
        return self._settings_origin().get(serial, {}).get(key)

    def saved_settings(self, serial: str) -> dict:
        return dict(self._settings_origin().get(serial, {}))

    def clear_saved_setting(self, serial: str, key: str) -> None:
        data = self.load_settings()
        origin = data.get("setting_origins", {})
        if serial in origin and key in origin[serial]:
            del origin[serial][key]
            self.save_settings(data)

    # ---------------------------------------------------------------- журнал

    def log(self, event: str, **fields) -> None:
        """Записать событие в журнал операций (без чувствительных данных)."""
        record = {
            "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "event": event,
        }
        for key, value in fields.items():
            if key in ("password", "content", "file_content", "token"):
                continue
            record[key] = value
        line = json.dumps(record, ensure_ascii=False)
        try:
            with self._lock:
                with open(self.log_file, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass

    def read_log(self, limit: int = 200) -> list[dict]:
        try:
            with open(self.log_file, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
