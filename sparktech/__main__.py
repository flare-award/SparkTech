"""Точка входа SparkTech.

Запускает локальный HTTP-сервер на 127.0.0.1 (только loopback) и открывает
интерфейс в нативном окне Windows (WebView2 через pywebview) либо, если
pywebview недоступен, в браузере по умолчанию. При закрытии окна сервер
останавливается — после выхода процесс не слушает порты.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser

from . import APP_NAME, __version__
from .core.store import Store
from .web.server import AppContext, create_server


def _find_free_port(preferred: int | None = None) -> int:
    if preferred:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", preferred))
                return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_window(url: str, app: AppContext):
    """Попытаться создать нативное окно. Возвращает вызываемый объект запуска."""
    try:
        import webview  # type: ignore
    except Exception:
        return None

    def start() -> None:
        window = webview.create_window(
            f"{APP_NAME} {__version__} — анализ и очистка Android",
            url,
            width=1240,
            height=840,
            min_size=(980, 660),
            background_color="#0b1120",
        )

        def on_closed() -> None:
            app.shutdown_event.set()

        window.events.closed += on_closed
        webview.start(debug=False)

    return start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sparktech",
        description="SparkTech — анализ хранилища и безопасная оптимизация Android через официальный ADB.",
    )
    parser.add_argument("--port", type=int, default=0, help="Порт локального интерфейса (по умолчанию свободный)")
    parser.add_argument("--browser", action="store_true", help="Открыть в браузере, даже если доступно нативное окно")
    parser.add_argument("--no-window", action="store_true", help="Не открывать ни окно, ни браузер (только сервер)")
    parser.add_argument("--adb", help="Путь к adb.exe", default=None)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Адрес локального сервера (по умолчанию 127.0.0.1; менять не рекомендуется)",
    )
    parser.add_argument("--data-dir", help="Каталог настроек и журнала", default=None)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    if sys.platform.startswith("win"):
        try:
            # Корректные DPI-масштабы в окне и браузере.
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    store = Store(args.data_dir) if args.data_dir else Store()
    app = AppContext(adb_path=args.adb, store=store)
    port = _find_free_port(args.port)
    server = create_server(app, port, host=args.host)
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{port}/"
    server_thread = threading.Thread(target=server.serve_forever, name="sparktech-http", daemon=True)
    server_thread.start()

    store.log("start", version=__version__, port=port)
    print(f"{APP_NAME} {__version__} — локальный интерфейс: {url}")
    print("Сервер доступен только с этого компьютера (127.0.0.1).")

    use_window = not args.browser and not args.no_window
    launcher = _build_window(url, app) if use_window else None

    if args.no_window:
        try:
            while not app.shutdown_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    elif launcher is not None:
        try:
            launcher()  # блокирует до закрытия окна
        except Exception as exc:  # pywebview может не найти WebView2 Runtime
            print(f"Не удалось открыть нативное окно ({exc}). Открываю браузер.")
            webbrowser.open(url)
            _wait_for_shutdown(app)
    else:
        if use_window:
            print(
                "Компонент нативного окна (pywebview/WebView2) недоступен — интерфейс открыт в браузере. "
                "Установите requirements-desktop.txt для окна приложения."
            )
        webbrowser.open(url)
        _wait_for_shutdown(app)

    store.log("stop", version=__version__)
    _shutdown(server, server_thread)
    return 0


def _wait_for_shutdown(app: AppContext) -> None:
    try:
        while not app.shutdown_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        app.shutdown_event.set()


def _shutdown(server, thread: threading.Thread) -> None:
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass
    thread.join(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
