"""Статические проверки интерфейса без браузера.

Проверяем связность разметки и клиентского кода: каждый id/селектор, к
которому обращается app.js, должен существовать в index.html. Это ловит
опечатки, которые иначе проявились бы только при запуске.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "sparktech" / "web" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")


def html_ids() -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', HTML))


def js_id_selectors() -> set[str]:
    return set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"', JS)) | set(
        re.findall(r'\$\$\("#([A-Za-z0-9_-]+)', JS)
    )


# id, которые создаются динамически в app.js (диалог подтверждения опасных действий)
DYNAMIC_IDS = {"confirm-risky"}


def test_all_js_ids_exist_in_html():
    missing = sorted(js_id_selectors() - html_ids() - DYNAMIC_IDS)
    assert not missing, f"app.js обращается к несуществующим id: {missing}"


def test_template_placeholder_is_safe():
    # Плейсхолдер не должен совпадать с именем JS-переменной, иначе замена
    # подстроки сломает скрипт.
    assert HTML.count("@@SPARKTECH_TOKEN@@") == 1
    assert "window.__SPARKTECH_TOKEN__" in HTML


def test_no_inline_event_handlers():
    # В разметке не должно быть onclick/onload — вся логика в app.js.
    assert not re.search(r'\son[a-z]+="', HTML)


def test_js_has_no_absolute_localhost_urls():
    """Клиент обязан использовать относительные URL: браузер пользователя
    может быть не в том же контейнере, что сервер."""
    bad = re.findall(r'https?://(?:127\.0\.0\.1|localhost)[^\s"\']*', JS)
    assert not bad, f"в app.js найдены абсолютные локальные адреса: {bad}"


def test_no_external_resources():
    """Страница не должна тянуть шрифты/скрипты из интернета."""
    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', HTML)
    assert not externals, f"внешние ресурсы в интерфейсе: {externals}"


def test_required_views_and_controls_present():
    for element_id in (
        "view-connect", "view-clean", "view-optimize", "view-log", "view-settings",
        "scan-start", "scan-cancel", "results-body", "filter-query", "filter-category",
        "modal-delete", "modal-confirm-check", "modal-delete-confirm",
        "optimize-run", "modal-optimize", "actions-list",
    ):
        assert element_id in html_ids(), f"нет элемента {element_id}"


def test_ui_is_russian():
    assert "Очистка" in HTML and "Оптимизация" in HTML and "Подключение" in HTML


def test_css_defines_key_classes_used_by_js():
    used = set(re.findall(r'class: `([a-z][a-z-]+)', JS)) | set(
        re.findall(r'class: "([a-z][a-z-]+)', JS)
    )
    missing = sorted(c for c in used if c and f".{c}" not in CSS)
    assert not missing, f"в CSS нет классов: {missing}"


def test_connection_states_documented():
    """Все четыре состояния подключения должны быть реализованы в ядре и UI."""
    from sparktech.core.adb import connection_status_of, parse_device_line

    assert connection_status_of([])[1].lower().startswith("телефон не подключён")
    assert "подтверждение" in connection_status_of([parse_device_line("A unauthorized")])[1]
    assert "готово" in connection_status_of([parse_device_line("A device")])[1]
    assert "недоступно" in connection_status_of([parse_device_line("A offline")])[1]
    # и UI умеет их отображать
    for key in ("ready", "unauthorized", "unavailable", "no_device"):
        assert key in JS
