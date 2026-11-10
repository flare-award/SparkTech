"""Общие фикстуры и «фальшивый» ADB для тестов без реального телефона."""

from __future__ import annotations

import pytest

from sparktech.core.adb import Device
from sparktech.core.models import RemoteFile
from sparktech.core.process import CommandResult


class FakeAdb:
    """Имитация AdbClient с предсказуемыми ответами.

    Важно: это только тестовая замена транспорта. Боевой код
    (Scanner/Deleter/Optimizer) остаётся тем же, что и с настоящим adb.
    """

    def __init__(self, serial: str = "TESTSERIAL", sdk: int = 33):
        self.serial = serial
        self.sdk = sdk
        self.commands: list[str] = []
        self.files: dict[str, RemoteFile] = {}
        self.fail_roots: set[str] = set()
        self.existing_roots: set[str] = set()
        self.hashes: dict[str, str] = {}
        self.settings_values: dict[str, str] = {}
        self.deleted: list[str] = []
        self.delete_fails: set[str] = set()
        self.changed_size: dict[str, int] = {}
        self.devices = [Device(serial=serial, state="device", model="Pixel Test", sdk=sdk)]

    # -------------------------------------------------------------- служебное

    def _res(self, out: str = "", code: int = 0, err: str = "") -> CommandResult:
        return CommandResult(argv=["adb"], returncode=code, stdout=out, stderr=err)

    # -------------------------------------------------------------- интерфейс

    def version(self) -> str:
        return "1.0.41"

    def version_diagnostics(self):
        return "1.0.41", None

    @staticmethod
    def normalize_user_path(raw: str) -> str:
        return raw.strip()

    def resolve(self) -> str:
        return "/fake/adb"

    def set_path(self, path: str) -> None:
        pass

    def start_server(self) -> None:
        pass

    def kill_server(self) -> None:
        pass

    def list_devices(self, with_props: bool = True):
        return self.devices

    def get_props(self, serial: str) -> dict[str, str]:
        return {
            "ro.product.model": "Pixel Test",
            "ro.product.manufacturer": "Google",
            "ro.build.version.sdk": str(self.sdk),
            "ro.build.version.release": "13",
            "ro.product.cpu.abi": "arm64-v8a",
        }

    def getprop(self, serial: str, key: str) -> str | None:
        return self.get_props(serial).get(key)

    def ensure_online(self, serial: str) -> None:
        return None

    def battery(self, serial: str) -> dict[str, str]:
        return {"level": "80"}

    def close(self) -> None:
        pass

    def run(self, args, *, timeout=None, cancel_event=None):
        cmd = " ".join(args)
        if "get-state" in cmd:
            return self._res("device")
        return self._res("")

    def shell(self, serial, command, *, timeout=None, cancel_event=None):
        self.commands.append(command)
        if cancel_event is not None and cancel_event.is_set():
            from sparktech.core.process import CommandCancelled

            raise CommandCancelled("Отменено")
        return self._shell(command)

    def shell_checked(self, serial, command, *, timeout=None, cancel_event=None):
        return self.shell(serial, command, timeout=timeout, cancel_event=cancel_event)

    def run_streaming(self, args, *, on_line, timeout=600, idle_timeout=90, cancel_event=None):
        command = args[-1] if args else ""
        self.commands.append(command)
        if cancel_event is not None and cancel_event.is_set():
            from sparktech.core.process import CommandCancelled

            raise CommandCancelled("Отменено")
        root = None
        for candidate in self.existing_roots:
            if f"'{candidate}'" in command:
                root = candidate
                break
        if root in self.fail_roots:
            from sparktech.core.process import CommandError

            raise CommandError("Permission denied")
        for path, file in self.files.items():
            if root and not path.startswith(root.rstrip("/") + "/"):
                continue
            if cancel_event is not None and cancel_event.is_set():
                from sparktech.core.process import CommandCancelled

                raise CommandCancelled("Отменено")
            on_line(f"{file.size}\x1f{file.mtime}\x1f{path}")
        return self._res("")

    # ------------------------------------------------------------ команды оболочки

    def _shell(self, command: str) -> CommandResult:
        if "df -k" in command:
            return self._res(
                "Filesystem 1K-blocks Used Available Use% Mounted on\n"
                "/dev/fuse 100000000 40000000 60000000 40% /storage/emulated/0\n"
            )
        if "-name .thumbnails" in command:
            return self._res("")
        if "if [ -e" in command:
            # как настоящая оболочка: каждое условие печатает только свой путь
            lines = [
                path
                for path in sorted(self.existing_roots)
                if f"[ -e '{path}' ]" in command
            ]
            return self._res("\n".join(lines))
        if "sha256sum" in command or "head -c 65536" in command:
            for path, digest in self.hashes.items():
                if f"'{path}'" in command:
                    return self._res(digest + "  -\n")
            return self._res("")
        if command.startswith("settings get global"):
            key = command.split()[-1]
            return self._res(self.settings_values.get(key, "1") + "\n")
        if command.startswith("settings put global"):
            parts = command.split()
            key, value = parts[3], parts[4].strip("'")
            self.settings_values[key] = value
            return self._res("")
        if command.startswith("rm -f --"):
            quoted = command[len("rm -f -- "):].strip()
            path = quoted[1:-1].replace("'\\''", "'")
            if path in self.delete_fails:
                return self._res("", 1, "Permission denied")
            self.deleted.append(path)
            self.files.pop(path, None)
            return self._res("")
        if command.startswith("[ -e"):
            quoted = command.split("[ -e ")[1].split(" ]")[0]
            path = quoted[1:-1].replace("'\\''", "'")
            return self._res("YES\n" if path in self.files else "NO\n")
        if command.startswith("if [ -L"):
            quoted = command.split("[ -L ")[1].split(" ]")[0]
            path = quoted[1:-1].replace("'\\''", "'")
            if path not in self.files:
                return self._res("MISSING\n")
            if path in self.changed_size:
                return self._res(f"{self.changed_size[path]}\n")
            return self._res(f"{self.files[path].size}\n")
        if "[ -x /system/bin/fstrim" in command:
            return self._res("YES\n")
        if command.startswith("pidof") or "pkill" in command:
            return self._res("4321\n")
        if "content call" in command:
            return self._res("0\n")
        if "cmd -l" in command:
            return self._res("activity\n")
        return self._res("")


@pytest.fixture
def fake_adb():
    adb = FakeAdb()
    adb.existing_roots = {
        "/storage/emulated/0/Download",
        "/storage/emulated/0/DCIM",
        "/storage/emulated/0/Android/obb",
    }
    return adb


@pytest.fixture
def sample_files():
    return {
        "/storage/emulated/0/Download/movie.mp4": RemoteFile(
            path="/storage/emulated/0/Download/movie.mp4", size=800 * 1024 * 1024, mtime=1700000000
        ),
        "/storage/emulated/0/Download/setup.apk": RemoteFile(
            path="/storage/emulated/0/Download/setup.apk", size=50 * 1024 * 1024, mtime=1700000000
        ),
        "/storage/emulated/0/Download/archive.zip": RemoteFile(
            path="/storage/emulated/0/Download/archive.zip", size=20 * 1024 * 1024, mtime=1700000000
        ),
        "/storage/emulated/0/Download/notes.txt": RemoteFile(
            path="/storage/emulated/0/Download/notes.txt", size=2000, mtime=1700000000
        ),
        "/storage/emulated/0/DCIM/photo.jpg": RemoteFile(
            path="/storage/emulated/0/DCIM/photo.jpg", size=300 * 1024 * 1024, mtime=1700000000
        ),
        "/storage/emulated/0/Android/obb/com.game/main.1.obb": RemoteFile(
            path="/storage/emulated/0/Android/obb/com.game/main.1.obb", size=900 * 1024 * 1024, mtime=1700000000
        ),
    }
