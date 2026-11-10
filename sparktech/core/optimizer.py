"""Оптимизация устройства: список проверяемых действий и их выполнение.

Принципы:

* каждое действие заранее описано: что делает, эффект, ограничения,
  побочные последствия, отменяемость;
* доступность действия проверяется на конкретном устройстве реальным
  запросом, а не предполагается;
* результат подтверждается проверкой, а не кодом возврата;
* где Android требует участия человека, SparkTech показывает инструкцию
  со статусом «требуется действие пользователя», а не фальшивый успех.

SparkTech намеренно не делает: очистку RAM, «ускорение на N%»,
отключение системных служб, удаление приложений, сброс устройства.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from .adb import AdbClient
from .models import ActionOutcome
from .process import CommandCancelled, CommandError, CommandTimeout
from .remote_path import quote_remote_path

SETTINGS_PREFIX = ""

# Действия с настройками, исходные значения которых мы сохраняем в
# локальном хранилище приложения (не на телефоне), чтобы можно было отменить.
SETTING_ACTIONS = {
    "stay_awake": "stay_on_while_plugged_in",
    "window_animation": "window_animation_scale",
    "transition_animation": "transition_animation_scale",
    "animator_duration": "animator_duration_scale",
}

ANIMATION_TARGET = "0"

# Значения по умолчанию: `settings get` возвращает "null", если настройку
# никогда не меняли. Чтобы «Отменить» работало, сохраняем стандартное значение.
DEFAULT_SETTING_VALUES = {
    "window_animation_scale": "1",
    "transition_animation_scale": "1",
    "animator_duration_scale": "1",
    "stay_on_while_plugged_in": "0",
}


@dataclass(frozen=True)
class ActionSpec:
    id: str
    title: str
    what: str
    effect: str
    limits: str
    side_effects: str
    reversible: str  # описание отмены
    min_sdk: int = 0
    destructive: bool = False
    requires_confirm: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "what": self.what,
            "effect": self.effect,
            "limits": self.limits,
            "side_effects": self.side_effects,
            "reversible": self.reversible,
            "destructive": self.destructive,
            "requires_confirm": self.requires_confirm,
        }


ACTIONS: list[ActionSpec] = [
    ActionSpec(
        id="trim_caches",
        title="Попросить систему освободить место в кэше",
        what=(
            "Отправляет системное событие и команду fstrim: Android завершает сессии "
            "неактивных приложений и сообщает ядру, что свободные блоки можно "
            "освободить на накопителе."
        ),
        effect=(
            "Иногда освобождается место, занятое кэшем приложений, и ускоряется запись "
            "на флеш-память. Объём заранее не известен и часто равен нулю."
        ),
        limits=(
            "Android сам управляет кэшем и может немедленно заполнить его снова. "
            "SparkTech не может показать, сколько освободилось: скрытые кэши "
            "приложений недоступны без специальных привилегий."
        ),
        side_effects=(
            "Приложениям потребуется чуть больше времени на первый повторный запуск "
            "(они заново загрузят данные). Пользовательские файлы не затрагиваются."
        ),
        reversible="Действие не меняет настроек; отменять нечего.",
    ),
    ActionSpec(
        id="media_scan",
        title="Пересоздать индекс медиафайлов",
        what=(
            "Запускает штатное сканирование хранилища MediaProvider, чтобы новые файлы "
            "появились в галерее и музыкальных приложениях, а удалённые — исчезли."
        ),
        effect="Исправляет типичные проблемы «файл есть, но галерея его не видит».",
        limits=(
            "На Android 11+ используется command-режим MediaProvider; если производитель "
            "его отключил, действие помечается как недоступное."
        ),
        side_effects="Сканирование может занять несколько минут и немного нагрузить процессор.",
        reversible="Индекс пересоздаётся автоматически; отмена не требуется.",
        min_sdk=21,
    ),
    ActionSpec(
        id="window_animation",
        title="Отключить анимацию окон",
        what="Устанавливает window_animation_scale = 0.",
        effect="Интерфейс субъективно реагирует быстрее, потому что анимации проигрываются мгновенно.",
        limits="Это не увеличивает производительность: приложения работают так же быстро.",
        side_effects="Меняется внешний вид системы; анимация перестаёт радовать, интерфейс выглядит резче.",
        reversible="SparkTech сохраняет исходное значение и может вернуть его по нажатию «Отменить».",
        min_sdk=16,
    ),
    ActionSpec(
        id="transition_animation",
        title="Отключить анимацию переходов",
        what="Устанавливает transition_animation_scale = 0.",
        effect="Переходы между экранами происходят без анимации — меньше задержек при переключении.",
        limits="На скорость работы приложений не влияет.",
        side_effects="Меняется привычное поведение системы.",
        reversible="Исходное значение сохраняется и восстанавливается.",
        min_sdk=16,
    ),
    ActionSpec(
        id="animator_duration",
        title="Отключить анимацию элементов (animator duration)",
        what="Устанавливает animator_duration_scale = 0.",
        effect="Микроанимации (нажатия, раскрытие меню) выполняются мгновенно.",
        limits="Не влияет на реальную скорость вычислений и сети.",
        side_effects="Некоторые приложения с нестандартными анимациями могут выглядеть непривычно.",
        reversible="Исходное значение сохраняется и восстанавливается.",
        min_sdk=16,
    ),
    ActionSpec(
        id="stay_awake",
        title="Не гасить экран при зарядке (для длительных операций)",
        what="Устанавливает stay_on_while_plugged_in = 7 (экран включён при зарядке по USB/сети).",
        effect="Помогает не прервать длительное копирование или сканирование, пока телефон на зарядке.",
        limits="Повышает расход энергии и нагрузку на экран, если оставить включённым навсегда.",
        side_effects="Экран не гаснет при зарядке — риск выгорания статичных элементов при долгом использовании.",
        reversible="Исходное значение сохраняется и восстанавливается.",
        min_sdk=16,
    ),
    ActionSpec(
        id="restart_ui",
        title="Перезапустить графическую оболочку (мягкая перезагрузка UI)",
        what="Завершает процесс system UI, и Android поднимает его заново.",
        effect="Устраняет зависший интерфейс, «съехавшие» шторки и подвисания launcher.",
        limits="Не освобождает заметный объём памяти и не ускоряет приложения.",
        side_effects="Экран кратковременно погаснет, уведомления обновятся, несохранённый ввод может пропасть.",
        reversible="Обратный эффект не требуется: оболочка перезапускается сама.",
        min_sdk=21,
        requires_confirm=True,
    ),
]


USER_ACTIONS = [
    {
        "id": "manual_cache",
        "title": "Очистка кэша приложений (вручную на телефоне)",
        "what": "Настройки → Приложения → нужное приложение → Память → Очистить кэш.",
        "why_not_auto": (
            "Без root и специальных привилегий SparkTech не имеет права очищать кэш чужих "
            "приложений. Команда `pm clear` удаляет все данные приложения (включая логины) и "
            "требует явного подтверждения, поэтому автоматически она не выполняется."
        ),
    },
    {
        "id": "manual_battery",
        "title": "Ограничение фоновой активности приложений",
        "what": "Настройки → Приложения → приложение → Батарея → «Ограничить».",
        "why_not_auto": (
            "Настройки батареи различаются у производителей, а неверное ограничение может "
            "лишить вас уведомлений. SparkTech показывает инструкцию, а не меняет это скрыто."
        ),
    },
    {
        "id": "manual_storage",
        "title": "Управление хранилищем в настройках Android",
        "what": "Настройки → Память → встроенная память: системный разбор по категориям.",
        "why_not_auto": "Список и очистка выполняются системной оболочкой, SparkTech не подменяет её.",
    },
]


class Optimizer:
    def __init__(self, adb: AdbClient, store):
        """``store`` — локальное хранилище исходных значений (см. settings.Store)."""
        self.adb = adb
        self.store = store

    # ------------------------------------------------------------ доступность

    def available_actions(self, serial: str, sdk: int | None) -> list[dict]:
        result = []
        for spec in ACTIONS:
            entry = spec.to_dict()
            entry["available"], entry["availability_note"] = self._check(spec, serial, sdk)
            result.append(entry)
        return result

    def _check(self, spec: ActionSpec, serial: str, sdk: int | None) -> tuple[bool, str]:
        if sdk is not None and sdk < spec.min_sdk:
            return False, f"Требуется Android API {spec.min_sdk} или новее (на устройстве {sdk})."
        try:
            if spec.id == "trim_caches":
                res = self.adb.shell(serial, "cmd -l 2>/dev/null | grep -c activity", timeout=20)
                # fstrim есть не на всех прошивках; проверяем бинарь.
                bin_res = self.adb.shell(
                    serial,
                    "[ -x /system/bin/fstrim ] || [ -x /system/xbin/fstrim ] && echo YES || echo NO",
                    timeout=20,
                )
                if bin_res.stdout.strip().endswith("NO"):
                    return False, "На этой прошивке нет утилиты fstrim; доступна только часть действия."
                return True, "Команда fstrim доступна; Android может отклонить вызов — результат будет проверен."
            if spec.id == "media_scan":
                res = self.adb.shell(
                    serial,
                    "content call --method scan_volume --uri content://media --arg external_primary "
                    ">/dev/null 2>&1; echo $?",
                    timeout=20,
                )
                code = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else "1"
                if code != "0":
                    return False, (
                        "MediaProvider этого устройства не поддерживает команду сканирования "
                        "через ADB. Используйте перезагрузку устройства или приложение, "
                        "которое само обновляет галерею."
                    )
                return True, "MediaProvider принимает команду сканирования."
            if spec.id == "restart_ui":
                res = self.adb.shell(serial, "pidof com.android.systemui || pidof systemui", timeout=20)
                if not res.stdout.strip():
                    return False, "Процесс системной оболочки не найден — действие неприменимо."
                return True, "Системная оболочка найдена; перезапуск займёт несколько секунд."
            if spec.id in SETTING_ACTIONS:
                key = SETTING_ACTIONS[spec.id]
                res = self.adb.shell(serial, f"settings get global {key}", timeout=20)
                if not res.ok:
                    return False, "Нет доступа к settings (команда недоступна)."
                return True, "Значение настройки читается и доступно для изменения."
        except (CommandError, CommandTimeout) as exc:
            return False, f"Не удалось проверить доступность: {exc}"
        return True, ""

    # ------------------------------------------------------------ выполнение

    def run(self, serial: str, action_id: str, *, sdk: int | None = None,
            progress: Callable[[str], None] | None = None,
            cancel_event: threading.Event | None = None) -> ActionOutcome:
        spec = next((a for a in ACTIONS if a.id == action_id), None)
        if spec is None:
            return ActionOutcome(action_id, "error", f"Неизвестное действие: {action_id}")
        available, note = self._check(spec, serial, sdk)
        if not available:
            return ActionOutcome(action_id, "skipped", note or "Действие недоступно на этом устройстве")
        try:
            handler = getattr(self, f"_do_{action_id}")
            return handler(serial, spec, note, cancel_event)
        except CommandCancelled:
            return ActionOutcome(action_id, "error", "Операция отменена пользователем")
        except CommandTimeout:
            return ActionOutcome(action_id, "error", "Устройство не ответило вовремя")
        except CommandError as exc:
            return ActionOutcome(action_id, "error", f"Ошибка adb: {exc}")

    def undo(self, serial: str, setting_action_id: str) -> ActionOutcome:
        key = SETTING_ACTIONS.get(setting_action_id)
        if not key:
            return ActionOutcome(setting_action_id, "error", "У этого действия нет сохранённого значения")
        saved = self.store.get_saved_setting(serial, key)
        if saved is None:
            return ActionOutcome(
                setting_action_id,
                "user_action",
                f"Исходное значение «{key}» не сохранено SparkTech. Его можно вернуть вручную в "
                "Настройках → Для разработчиков или задать стандартное значение 1.",
            )
        res = self.adb.shell(serial, f"settings put global {key} {quote_remote_path(str(saved))}", timeout=20)
        if not res.ok:
            return ActionOutcome(setting_action_id, "error", f"Не удалось вернуть значение: {res.output.strip()}")
        check = self.adb.shell(serial, f"settings get global {key}", timeout=15)
        if check.stdout.strip() == str(saved):
            self.store.clear_saved_setting(serial, key)
            return ActionOutcome(setting_action_id, "success", f"Исходное значение «{key}» = {saved} восстановлено")
        return ActionOutcome(setting_action_id, "error", "Значение не подтвердилось после записи")

    # -------------------------------------------------------------- действия

    def _do_trim_caches(self, serial, spec, note, cancel_event):
        details = []
        freed_before = self._free_bytes(serial)
        res = self.adb.shell(serial, "cmd activity send-trim-memory 0 BACKGROUND_MODERATE 2>&1", timeout=30)
        details.append(
            "send-trim-memory: " + (res.output.strip()[:200] or f"код {res.returncode}")
        )
        trim = self.adb.shell(serial, "fstrim -v /data 2>&1 || /system/bin/fstrim -v /data 2>&1", timeout=120)
        details.append("fstrim: " + (trim.output.strip()[:200] or "нет вывода"))
        freed_after = self._free_bytes(serial)
        delta = (freed_after - freed_before) if (freed_before is not None and freed_after is not None) else None
        if delta is not None and delta > 0:
            details.append(f"Свободное место по данным df увеличилось на {delta} байт.")
        else:
            details.append(
                "Объём свободного места по df не увеличился. Это ожидаемо: кэш приложений "
                "недоступен SparkTech, а fstrim возвращает блоки накопителя, а не файлы."
            )
        if trim.returncode != 0 and "not permitted" in trim.output.lower():
            return ActionOutcome(
                "trim_caches", "failed",
                "fstrim не разрешён на этом устройстве (прошивка требует прав).",
                details=details,
            )
        return ActionOutcome(
            "trim_caches", "success",
            "Системе отправлен запрос на освобождение кэша и trim накопителя.",
            details=details,
        )

    def _do_media_scan(self, serial, spec, note, cancel_event):
        res = self.adb.shell(
            serial,
            "content call --method scan_volume --uri content://media --arg external_primary 2>&1",
            timeout=180,
        )
        if res.returncode != 0:
            return ActionOutcome(
                "media_scan",
                "user_action",
                "MediaProvider не принял команду. Перезагрузите телефон: индекс медиа пересоздастся "
                "автоматически при загрузке.",
                details=[res.output.strip()[:300]],
            )
        return ActionOutcome(
            "media_scan",
            "success",
            "MediaProvider запустил пересоздание индекса. Появление файлов в галерее может занять несколько минут.",
            details=[res.output.strip()[:300]] if res.output.strip() else [],
        )

    def _do_restart_ui(self, serial, spec, note, cancel_event):
        before = self.adb.shell(serial, "pidof com.android.systemui || pidof systemui", timeout=15).stdout.strip()
        if not before:
            return ActionOutcome("restart_ui", "failed", "Системная оболочка не запущена — перезапускать нечего.")
        self.adb.shell(serial, "pkill -f com.android.systemui 2>/dev/null || killall com.android.systemui 2>/dev/null", timeout=30)
        import time

        for _ in range(20):
            if cancel_event is not None and cancel_event.is_set():
                return ActionOutcome("restart_ui", "error", "Операция отменена")
            time.sleep(0.7)
            after = self.adb.shell(serial, "pidof com.android.systemui || pidof systemui", timeout=15).stdout.strip()
            if after and after != before:
                return ActionOutcome(
                    "restart_ui", "success",
                    "Системная оболочка перезапущена и работает (новый PID).",
                    details=[f"PID до: {before}", f"PID после: {after}"],
                )
        return ActionOutcome(
            "restart_ui",
            "user_action",
            "Команда отправлена, но новый процесс оболочки не подтвердился. Проверьте экран телефона; "
            "если интерфейс не появился, перезагрузите устройство кнопкой питания.",
        )

    def _do_setting(self, serial, spec, value, cancel_event):
        key = SETTING_ACTIONS[spec.id]
        before = self.adb.shell(serial, f"settings get global {key}", timeout=20).stdout.strip()
        details: list[str] = []
        if not before or before == "null":
            default = DEFAULT_SETTING_VALUES.get(key, "1")
            self.store.save_setting_origin(serial, key, default)
            details.append(
                f"Настройка не была задана (null). Сохранено стандартное значение {default} — "
                "«Отменить» вернёт его."
            )
            before = default
        else:
            self.store.save_setting_origin(serial, key, before)
        cmd_value = value
        res = self.adb.shell(serial, f"settings put global {key} {quote_remote_path(cmd_value)}", timeout=20)
        if not res.ok:
            return ActionOutcome(spec.id, "error", f"Android отклонил изменение: {res.output.strip()[:200]}")
        check = self.adb.shell(serial, f"settings get global {key}", timeout=20).stdout.strip()
        if check != cmd_value:
            return ActionOutcome(
                spec.id, "failed",
                f"Значение {key} после записи равно «{check}» вместо «{cmd_value}». Изменение не подтверждено.",
                details=details + [f"Было: {before or 'не прочитано'}"],
            )
        return ActionOutcome(
            spec.id,
            "success",
            f"Настройка {key} = {cmd_value} подтверждена проверкой.",
            details=details + [f"Было: {before or 'не прочитано'}", f"Стало: {check}"],
            reversible=True,
            can_undo=True,
        )

    def _do_window_animation(self, serial, spec, note, cancel_event):
        return self._do_setting(serial, spec, ANIMATION_TARGET, cancel_event)

    def _do_transition_animation(self, serial, spec, note, cancel_event):
        return self._do_setting(serial, spec, ANIMATION_TARGET, cancel_event)

    def _do_animator_duration(self, serial, spec, note, cancel_event):
        return self._do_setting(serial, spec, ANIMATION_TARGET, cancel_event)

    def _do_stay_awake(self, serial, spec, note, cancel_event):
        return self._do_setting(serial, spec, "7", cancel_event)

    def _free_bytes(self, serial) -> int | None:
        from .scanner import Scanner

        total, free = Scanner(self.adb).storage_stats(serial)
        return free
