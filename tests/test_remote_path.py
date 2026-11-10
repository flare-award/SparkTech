"""Безопасность путей: инъекции в оболочку и защита системных каталогов."""

import pytest

from sparktech.core.remote_path import (
    UnsafePathError,
    is_protected,
    is_safe_deletable,
    normalize_remote,
    quote_remote_path,
    strip_sdcard_prefix,
)


@pytest.mark.parametrize(
    "path",
    [
        "/storage/emulated/0/Download/my file.mp4",
        "/storage/emulated/0/Download/файл с пробелом.zip",
        "/storage/emulated/0/Download/$(reboot).apk",
        "/storage/emulated/0/Download/a;rm -rf /sdcard;.txt",
        "/storage/emulated/0/Download/it's a name.log",
        "/storage/emulated/0/Download/`id`*?[].tmp",
        "/storage/emulated/0/Download/名称.文件",
    ],
)
def test_quote_blocks_shell_injection(path):
    quoted = quote_remote_path(path)
    assert quoted.startswith("'") and quoted.endswith("'")
    # Внутри одиночных кавычек оболочка не исполняет подстановки.
    # Проверяем, что каждая одинарная кавычка закрыта/экранирована.
    inner = quoted[1:-1]
    assert "'" not in inner.replace("'\\''", "")
    # Реальное поведение оболочки проверяет test_shell_roundtrip ниже.


@pytest.mark.parametrize(
    "path",
    [
        "/storage/emulated/0/Download/my file.mp4",
        "/storage/emulated/0/Download/файл.zip",
        "/storage/emulated/0/Download/it's.log",
        "/storage/emulated/0/Download/$(reboot).apk",
    ],
)
def test_shell_roundtrip_returns_exact_path(path):
    """Проверяем на настоящей /bin/sh, что кавычки дают ровно исходный путь."""
    import subprocess

    out = subprocess.run(
        ["sh", "-c", f"printf %s {quote_remote_path(path)}"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert out == path


def test_quote_rejects_empty_and_control_chars():
    with pytest.raises(UnsafePathError):
        quote_remote_path("")
    with pytest.raises(UnsafePathError):
        quote_remote_path("/a\x00b")
    with pytest.raises(UnsafePathError):
        quote_remote_path("/a\rb")


def test_normalize_remote():
    assert normalize_remote("storage/emulated/0/Download/") == "/storage/emulated/0/Download"
    assert normalize_remote("/sdcard//a/./b/../c") == "/sdcard/a/c"
    assert normalize_remote("/../../etc") == "/etc"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/storage",
        "/storage/emulated",
        "/data",
        "/data/data/com.example.app/files/x.db",
        "/system/bin/sh",
        "/system",
        "/vendor/lib64/libc.so",
        "/storage/emulated/0/Android/data/com.example/cache/a.tmp",
        "/storage/emulated/0/Android/obb/com.game/main.obb",
        "/sdcard/Android/data/com.x/file",
    ],
)
def test_protected_paths_are_refused(path):
    allowed, reason = is_safe_deletable(path)
    assert not allowed, f"путь {path} не должен быть удаляемым"
    assert reason
    assert is_protected(path)


@pytest.mark.parametrize(
    "path",
    [
        "/storage/emulated/0/Download/setup.apk",
        "/storage/emulated/0/DCIM/photo.jpg",
        "/sdcard/Download/a b c.zip",
    ],
)
def test_user_paths_are_allowed(path):
    allowed, reason = is_safe_deletable(path)
    assert allowed, reason


def test_unnormalized_and_relative_paths_refused():
    assert not is_safe_deletable("/storage/emulated/0/Download/a/../b.mp4")[0]
    assert not is_safe_deletable("storage/emulated/0/Download/b.mp4")[0]
    assert not is_safe_deletable("/storage/emulated/0/Download/\n")[0]


def test_strip_sdcard_prefix():
    assert strip_sdcard_prefix("/sdcard/Download/a") == "/storage/emulated/0/Download/a"
    assert strip_sdcard_prefix("/storage/emulated/0/Download/a") == "/storage/emulated/0/Download/a"
