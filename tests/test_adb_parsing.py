"""Разбор вывода adb: состояния устройств, свойства, строки find."""

from sparktech.core.adb import Device, connection_status_of, parse_device_line, parse_getprop
from sparktech.core.scanner import parse_find_line


def test_parse_ready_device_line():
    line = "9889cc30564c device product:beyond1lte model:SM_G973F device:beyond1 transport_id:3"
    d = parse_device_line(line)
    assert isinstance(d, Device)
    assert d.serial == "9889cc30564c"
    assert d.state == "device"
    assert d.ready
    assert d.model == "SM G973F"
    assert d.transport_id == "3"


def test_parse_unauthorized_device():
    d = parse_device_line("R58M12345 unauthorized usb:1-2 transport_id:5")
    assert d.state == "unauthorized"
    assert d.needs_authorization
    assert not d.ready


def test_parse_no_permissions_state_with_space():
    d = parse_device_line("abc123 no permissions (missing udev rules?); see x")
    assert d.state == "no permissions"
    assert d.serial == "abc123"


def test_parse_offline_and_unknown():
    assert parse_device_line("XYZ offline").state == "offline"
    assert parse_device_line("XYZ weird_state").state == "unknown"


def test_parse_empty_line():
    assert parse_device_line("") is None
    assert parse_device_line("   ") is None


def test_display_name_and_labels():
    d = parse_device_line("S1 device model:Pixel_7 transport_id:1")
    assert d.display_name.startswith("Pixel 7")
    assert d.state_label == "Подключено"
    assert d.to_dict()["ready"] is True


def test_parse_getprop():
    text = "[ro.product.model]: [Pixel 7]\n[ro.build.version.sdk]: [33]\n[junk]\n"
    props = parse_getprop(text)
    assert props["ro.product.model"] == "Pixel 7"
    assert props["ro.build.version.sdk"] == "33"
    assert len(props) == 2


def test_parse_find_line():
    line = "12345\x1f1700000000\x1f/storage/emulated/0/Download/a b.mp4"
    f = parse_find_line(line, root="/storage/emulated/0/Download")
    assert f.path == "/storage/emulated/0/Download/a b.mp4"
    assert f.size == 12345
    assert f.mtime == 1700000000
    assert f.name == "a b.mp4"
    assert f.extension == ".mp4"


def test_parse_find_line_bad_input():
    assert parse_find_line("garbage") is None
    assert parse_find_line("12\x1f34\x1frelative/path") is None
    assert parse_find_line("abc\x1f34\x1f/storage/a") is None


def test_parse_find_line_cyrillic_and_spaces():
    line = "777\x1f1600000000\x1f/storage/emulated/0/Download/фото кот (1).jpg"
    f = parse_find_line(line)
    assert f.name == "фото кот (1).jpg"


def test_connection_status_of():
    ready = [parse_device_line("A device")]
    assert connection_status_of(ready)[0] == "ready"
    assert connection_status_of([])[0] == "no_device"
    assert connection_status_of([parse_device_line("B unauthorized")])[0] == "unauthorized"
    assert connection_status_of([parse_device_line("C offline")])[0] == "unavailable"
    assert connection_status_of(ready + [parse_device_line("D device")])[0] == "ready_multiple"
