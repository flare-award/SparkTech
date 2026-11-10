"""Обёртка над официальным Android Debug Bridge.

SparkTech не скачивает adb самостоятельно. Приложение ищет уже
установленные официальные Platform-Tools (или сборку, вложенную в
дистрибутив при запуске сборки) и честно сообщает, если их нет.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import process
from .process import CommandResult, CommandError, CommandTimeout, CommandCancelled

# Состояния, которые adb сообщает в `adb devices`
DEVICE_STATE_LABELS = {
    "device": "Подключено",
    "unauthorized": "Ожидает подтверждения отладки",
    "offline": "Устройство недоступно",
    "authorizing": "Идёт авторизация",
    "recovery": "Загружено в recovery",
    "sideload": "Режим sideload",
    "bootloader": "Режим загрузчика",
    "no permissions": "Нет прав доступа к USB-устройству",
    "connecting": "Идёт подключение",
    "unknown": "Неизвестное состояние",
}

ADB_DOWNLOAD_URL = "https://developer.android.com/tools/releases/platform-tools"
ADB_LICENSE_NOTE = (
    "Android SDK Platform-Tools распространяются Google по лицензии Android SDK "
    "License Agreement и не входят в репозиторий SparkTech. Скачайте их только с "
    "официального сайта developer.android.com."
)


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    model: str | None = None
    product: str | None = None
    device: str | None = None
    transport_id: str | None = None
    usb: str | None = None
    manufacturer: str | None = None
    android_version: str | None = None
    sdk: int | None = None
    abi: str | None = None

    @property
    def ready(self) -> bool:
        return self.state == "device"

    @property
    def needs_authorization(self) -> bool:
        return self.state in ("unauthorized", "authorizing")

    @property
    def state_label(self) -> str:
        return DEVICE_STATE_LABELS.get(self.state, DEVICE_STATE_LABELS["unknown"])

    @property
    def display_name(self) -> str:
        base = self.model or self.product or self.serial
        return f"{base} ({self.serial})" if self.model else self.serial

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "state": self.state,
            "state_label": self.state_label,
            "ready": self.ready,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "product": self.product,
            "device": self.device,
            "transport_id": self.transport_id,
            "usb": self.usb,
            "android_version": self.android_version,
            "sdk": self.sdk,
            "abi": self.abi,
            "display_name": self.display_name,
        }


class AdbNotFound(CommandError):
    """adb не найден на компьютере."""


class AdbClient:
    def __init__(self, adb_path: str | None = None, *, timeout: float = 30.0):
        self._adb_path = adb_path
        self.default_timeout = timeout
        self._server_checked = False

    # ------------------------------------------------------------------ пути

    @staticmethod
    def candidate_paths() -> list[Path]:
        """Где искать официальный adb (в порядке приоритета)."""
        names = ["adb.exe", "adb"] if os.name == "nt" else ["adb"]
        candidates: list[Path] = []

        env_path = os.environ.get("SPARKTECH_ADB")
        if env_path:
            candidates.append(Path(env_path))

        # Каталог, рядом с которым лежит приложение (сборка .exe или исходники).
        if getattr(sys, "frozen", False):
            base_dirs = [Path(sys.executable).parent]
        else:
            base_dirs = [Path(__file__).resolve().parents[2]]
        for base in base_dirs:
            for rel in ("vendor/platform-tools", "platform-tools", "tools/platform-tools"):
                for name in names:
                    candidates.append(base / rel / name)

        # Стандартные места установки Android SDK.
        sdk_roots = [
            os.environ.get("ANDROID_HOME"),
            os.environ.get("ANDROID_SDK_ROOT"),
            os.path.expanduser("~/AppData/Local/Android/Sdk") if os.name == "nt" else None,
            os.path.expanduser("~/Android/Sdk"),
        ]
        for root in sdk_roots:
            if root:
                for name in names:
                    candidates.append(Path(root) / "platform-tools" / name)

        found = process.which("adb")
        if found:
            candidates.append(Path(found))
        return candidates

    def resolve(self, force: bool = False) -> str:
        if self._adb_path and not force and Path(self._adb_path).exists():
            return self._adb_path
        for candidate in self.candidate_paths():
            try:
                if candidate.is_file():
                    self._adb_path = str(candidate)
                    return self._adb_path
            except OSError:
                continue
        raise AdbNotFound(
            "adb не найден: официальный Android SDK Platform-Tools не обнаружен на компьютере. "
            f"Скачайте его с {ADB_DOWNLOAD_URL} и укажите путь в настройках SparkTech."
        )

    def set_path(self, path: str) -> None:
        self._adb_path = path.strip() or None
        self._server_checked = False

    @property
    def adb_path(self) -> str:
        return self.resolve()

    def _argv(self, args: Iterable[str]) -> list[str]:
        return [self.resolve(), *[str(a) for a in args]]

    def _resolve_argv_for_stream(self, args: Iterable[str]) -> list[str]:
        return self._argv(args)

    def run_streaming(
        self,
        args: Iterable[str],
        *,
        on_line,
        timeout: float = 600.0,
        idle_timeout: float = 90.0,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return process.run_streaming(
            self._argv(args),
            on_line=on_line,
            timeout=timeout,
            idle_timeout=idle_timeout,
            cancel_event=cancel_event,
            env=self._env(),
        )


    def _env(self) -> dict[str, str]:
        env = {}
        try:
            env.update(process.popen_env_with_bin_dir(str(Path(self.resolve()).parent)))
        except AdbNotFound:
            pass
        return env

    # ------------------------------------------------------------------ вызов

    def run(
        self,
        args: Iterable[str],
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return process.run(
            self._argv(args),
            timeout=timeout if timeout is not None else self.default_timeout,
            cancel_event=cancel_event,
            env=self._env(),
        )

    def run_checked(
        self,
        args: Iterable[str],
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        res = self.run(args, timeout=timeout, cancel_event=cancel_event)
        if not res.ok:
            raise CommandError(res.output.strip() or f"adb завершился с кодом {res.returncode}")
        return res

    def version(self) -> str | None:
        try:
            res = self.run(["version"], timeout=15)
        except (CommandError, OSError):
            return None
        if not res.ok:
            return None
        m = re.search(r"Android Debug Bridge version ([\d.]+)", res.stdout)
        return m.group(1) if m else (res.stdout.splitlines() or [""])[0].strip()

    def start_server(self) -> None:
        """Запустить adb-сервер (он нужен для любых операций)."""
        if self._server_checked:
            return
        res = self.run(["start-server"], timeout=40)
        # adb может писать '* daemon started successfully' в stderr при коде 0.
        if not res.ok and "daemon" not in (res.stdout + res.stderr).lower():
            raise CommandError(res.output.strip() or "Не удалось запустить adb-сервер")
        self._server_checked = True

    def kill_server(self) -> None:
        try:
            self.run(["kill-server"], timeout=15)
        except (CommandError, OSError):
            pass
        self._server_checked = False

    # --------------------------------------------------------------- устройства

    def list_devices(self, *, with_props: bool = True) -> list[Device]:
        """Получить список устройств.

        Информация о модели берётся из ``adb devices -l`` и, для готовых
        устройств, из ``getprop``. Ошибка у одного устройства не должна
        ломать весь список.
        """
        try:
            res = self.run(["devices", "-l"], timeout=25)
        except CommandCancelled:
            raise
        except CommandTimeout as exc:
            raise CommandError("adb не ответил на запрос списка устройств") from exc
        if res.returncode != 0 and not res.stdout.strip():
            raise CommandError(res.output.strip() or "Не удалось получить список устройств")

        devices: list[Device] = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parsed = parse_device_line(line)
            if parsed is None:
                continue
            if with_props and parsed.ready:
                parsed = self._enrich(parsed)
            devices.append(parsed)
        return devices

    def _enrich(self, device: Device) -> Device:
        props = self.get_props(device.serial)
        if not props:
            return device
        sdk_raw = props.get("ro.build.version.sdk", "")
        try:
            sdk = int(sdk_raw)
        except ValueError:
            sdk = None
        return Device(
            serial=device.serial,
            state=device.state,
            model=props.get("ro.product.model") or device.model,
            product=device.product,
            device=device.device,
            transport_id=device.transport_id,
            usb=device.usb,
            manufacturer=props.get("ro.product.manufacturer") or device.manufacturer,
            android_version=props.get("ro.build.version.release"),
            sdk=sdk,
            abi=props.get("ro.product.cpu.abi"),
        )

    def get_props(self, serial: str) -> dict[str, str]:
        try:
            res = self.run(["-s", serial, "shell", "getprop"], timeout=20)
        except (CommandError, OSError):
            return {}
        if not res.ok:
            return {}
        return parse_getprop(res.stdout)

    def getprop(self, serial: str, key: str) -> str | None:
        try:
            res = self.run(["-s", serial, "shell", "getprop", key], timeout=15)
        except CommandError:
            return None
        value = res.stdout.strip()
        return value or None

    def shell(
        self,
        serial: str,
        command: str,
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        """Выполнить команду оболочки на устройстве.

        ``command`` — одна строка, которую мы формируем сами из
        экранированных путей (см. ``remote_path.quote_remote_path``).
        Внешние данные никогда не подставляются иначе как через
        экранирование.
        """
        return self.run(
            ["-s", serial, "shell", command],
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def shell_checked(
        self,
        serial: str,
        command: str,
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        res = self.shell(serial, command, timeout=timeout, cancel_event=cancel_event)
        if not res.ok:
            raise CommandError(res.output.strip() or f"Команда завершилась с кодом {res.returncode}")
        return res

    def ensure_online(self, serial: str) -> None:
        """Проверить, что устройство всё ещё подключено и авторизовано."""
        res = self.run(["-s", serial, "get-state"], timeout=15)
        state = res.stdout.strip()
        if not res.ok or state != "device":
            raise CommandError(
                "Устройство больше не доступно. Проверьте кабель и подтверждение отладки."
            )

    def battery(self, serial: str) -> dict[str, str]:
        res = self.shell(serial, "dumpsys battery", timeout=20)
        if not res.ok:
            return {}
        data: dict[str, str] = {}
        for line in res.stdout.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                data[key.strip().lower().replace(" ", "_")] = value.strip()
        return data

    def close(self) -> None:
        self.kill_server()


# ---------------------------------------------------------------------- парсинг


def parse_getprop(text: str) -> dict[str, str]:
    """Разобрать вывод ``getprop`` вида ``[key]: [value]``."""
    props: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\[([^\]]+)\]:\s*\[(.*)\]\s*$", line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props


def parse_device_line(line: str) -> Device | None:
    """Разобрать одну строку ``adb devices -l``.

    Пример::

        9889cc30564c device product:beyond1lte model:SM_G973F device:beyond1 transport_id:3

    Состояние может содержать пробел (``no permissions``).
    """
    line = line.strip()
    if not line:
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    serial = parts[0]

    state = None
    rest_index = 1
    for size in (3, 2, 1):
        candidate = " ".join(parts[1:1 + size])
        if candidate in DEVICE_STATE_LABELS:
            state = candidate
            rest_index = 1 + size
            break
    if state is None:
        # Неизвестное состояние — берём первое слово и помечаем как unknown.
        state = parts[1] if len(parts) > 1 else "unknown"
        if state not in DEVICE_STATE_LABELS:
            state = "unknown"
        rest_index = 2

    attrs: dict[str, str] = {}
    for token in parts[rest_index:]:
        if ":" in token:
            key, _, value = token.partition(":")
            attrs[key] = value

    return Device(
        serial=serial,
        state=state,
        model=attrs.get("model", "").replace("_", " ") or None,
        product=attrs.get("product"),
        device=attrs.get("device"),
        transport_id=attrs.get("transport_id"),
        usb=attrs.get("usb"),
    )


def connection_status_of(devices: Iterable[Device]) -> tuple[str, str]:
    """Сводный статус подключения для интерфейса.

    Возвращает (код, подпись). Коды: no_device, unauthorized, unavailable,
    ready, ready_multiple.
    """
    devices = list(devices)
    if not devices:
        return "no_device", "Телефон не подключён"
    ready = [d for d in devices if d.ready]
    if ready:
        if len(ready) > 1:
            return "ready_multiple", f"Готово к работе устройств: {len(ready)}. Выберите нужное."
        return "ready", "Устройство подключено и готово"
    if any(d.needs_authorization for d in devices):
        return "unauthorized", "Устройство обнаружено, ожидается подтверждение отладки"
    return "unavailable", "Устройство недоступно или возникла ошибка"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
