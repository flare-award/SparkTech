"""Оптимизация: доступность действий, проверка результата, отмена."""

from sparktech.core.optimizer import ACTIONS, Optimizer
from tests.conftest import FakeAdb


class MemoryStore:
    def __init__(self):
        self.saved = {}

    def save_setting_origin(self, serial, key, value):
        self.saved.setdefault((serial, key), value)

    def get_saved_setting(self, serial, key):
        return self.saved.get((serial, key))

    def saved_settings(self, serial):
        return {k[1]: v for k, v in self.saved.items() if k[0] == serial}

    def clear_saved_setting(self, serial, key):
        self.saved.pop((serial, key), None)


def test_action_descriptions_are_complete():
    for a in ACTIONS:
        for field in ("what", "effect", "limits", "side_effects", "reversible"):
            assert getattr(a, field).strip(), f"{a.id}.{field} пуст"
    # Никаких обещаний «ускорения на 100%»
    text = " ".join(a.effect + a.limits for a in ACTIONS).lower()
    assert "100%" not in text


def test_availability_lists_all_actions():
    adb = FakeAdb()
    opt = Optimizer(adb, MemoryStore())
    actions = opt.available_actions("TESTSERIAL", 33)
    assert len(actions) == len(ACTIONS)
    assert all("available" in a and "availability_note" in a for a in actions)


def test_animation_setting_applies_and_is_verified():
    adb = FakeAdb()
    store = MemoryStore()
    opt = Optimizer(adb, store)
    outcome = opt.run("TESTSERIAL", "window_animation", sdk=33)
    assert outcome.status == "success"
    assert adb.settings_values["window_animation_scale"] == "0"
    assert store.get_saved_setting("TESTSERIAL", "window_animation_scale") == "1"
    assert outcome.can_undo


def test_setting_reports_failure_when_not_confirmed():
    adb = FakeAdb()

    class Stubborn(FakeAdb):
        def _shell(self, command):
            if command.startswith("settings put"):
                return self._res("")
            return super()._shell(command)

        def shell(self, serial, command, *, timeout=None, cancel_event=None):
            if command.startswith("settings get"):
                return self._res("1\n")
            return super().shell(serial, command, timeout=timeout, cancel_event=cancel_event)

    opt = Optimizer(Stubborn(), MemoryStore())
    outcome = opt.run("TESTSERIAL", "window_animation", sdk=33)
    assert outcome.status == "failed"
    assert "не подтверждено" in outcome.message.lower()


def test_undo_restores_saved_value():
    adb = FakeAdb()
    store = MemoryStore()
    opt = Optimizer(adb, store)
    opt.run("TESTSERIAL", "window_animation", sdk=33)
    outcome = opt.undo("TESTSERIAL", "window_animation")
    assert outcome.status == "success"
    assert adb.settings_values["window_animation_scale"] == "1"
    assert store.get_saved_setting("TESTSERIAL", "window_animation_scale") is None


def test_undo_without_saved_value_is_user_action():
    adb = FakeAdb()
    opt = Optimizer(adb, MemoryStore())
    outcome = opt.undo("TESTSERIAL", "window_animation")
    assert outcome.status == "user_action"
    assert "не сохранено" in outcome.message


def test_unavailable_action_is_skipped():
    adb = FakeAdb()

    class NoMedia(FakeAdb):
        def _shell(self, command):
            if "content call" in command:
                return self._res("1\n")
            return super()._shell(command)

    opt = Optimizer(NoMedia(), MemoryStore())
    outcome = opt.run("TESTSERIAL", "media_scan", sdk=33)
    assert outcome.status == "skipped"


def test_old_android_limits_actions():
    adb = FakeAdb()
    opt = Optimizer(adb, MemoryStore())
    actions = {a["id"]: a for a in opt.available_actions("TESTSERIAL", 10)}
    assert actions["media_scan"]["available"] is False
    assert "API" in actions["media_scan"]["availability_note"]


def test_unknown_action_is_error():
    opt = Optimizer(FakeAdb(), MemoryStore())
    outcome = opt.run("TESTSERIAL", "definitely_not_an_action")
    assert outcome.status == "error"


def test_trim_caches_does_not_claim_freed_bytes():
    adb = FakeAdb()
    opt = Optimizer(adb, MemoryStore())
    outcome = opt.run("TESTSERIAL", "trim_caches", sdk=33)
    assert outcome.status in ("success", "failed")
    joined = " ".join(outcome.details).lower()
    assert "не увеличился" in joined or "увеличилось" in joined


def test_undo_works_when_setting_was_null():
    """Если настройку никогда не меняли (null), отмена возвращает стандарт."""
    adb = FakeAdb()
    adb.settings_values["window_animation_scale"] = "null"
    store = MemoryStore()
    opt = Optimizer(adb, store)
    outcome = opt.run("TESTSERIAL", "window_animation", sdk=33)
    assert outcome.status == "success"
    assert any("стандартное значение" in d for d in outcome.details)
    assert store.get_saved_setting("TESTSERIAL", "window_animation_scale") == "1"
    undo = opt.undo("TESTSERIAL", "window_animation")
    assert undo.status == "success"
    assert adb.settings_values["window_animation_scale"] == "1"
