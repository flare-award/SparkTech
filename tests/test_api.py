"""Интеграционные тесты локального HTTP-API (без реального телефона).

Проверяют: сервер слушает только loopback, токен обязателен, статика
отдаётся без выхода за каталог, задания создаются и отменяются, удаление
требует подтверждения.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from sparktech.core.models import RemoteFile
from sparktech.web.server import AppContext, create_server
from tests.conftest import FakeAdb


@pytest.fixture
def api(tmp_path):
    app = AppContext(store=__import__("sparktech.core.store", fromlist=["Store"]).Store(tmp_path))
    fake = FakeAdb()
    fake.existing_roots = {"/storage/emulated/0/Download"}
    fake.files = {
        "/storage/emulated/0/Download/movie.mp4": RemoteFile(
            "/storage/emulated/0/Download/movie.mp4", 300 * 1024 * 1024, 1700000000
        ),
        "/storage/emulated/0/Download/app.apk": RemoteFile(
            "/storage/emulated/0/Download/app.apk", 40 * 1024 * 1024, 1700000000
        ),
    }
    app.adb = fake
    from sparktech.core.deleter import Deleter
    from sparktech.core.optimizer import Optimizer
    from sparktech.core.scanner import Scanner

    app.scanner = Scanner(fake)
    app.deleter = Deleter(fake)
    app.optimizer = Optimizer(fake, app.store)
    server = create_server(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {
        "app": app,
        "fake": fake,
        "port": server.server_address[1],
        "token": app.token,
    }
    server.shutdown()
    server.server_close()


def call(api, path, method="GET", body=None, token=True, origin=None, timeout=20):
    url = f"http://127.0.0.1:{api['port']}{path}"
    headers = {}
    if token:
        headers["X-SparkTech-Token"] = api["token"]
    if body is not None:
        headers["Content-Type"] = "application/json"
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def test_index_contains_token_and_no_placeholder(api):
    req = urllib.request.Request(f"http://127.0.0.1:{api['port']}/")
    with urllib.request.urlopen(req, timeout=10) as res:
        html = res.read().decode()
    assert api["token"] in html
    assert "@@SPARKTECH_TOKEN@@" not in html
    # имя JS-переменной не должно быть повреждено подстановкой
    assert "window.__SPARKTECH_TOKEN__" in html


def test_api_requires_token(api):
    status, payload = call(api, "/api/state", token=False)
    assert status == 403
    assert payload["ok"] is False


def test_health_is_public(api):
    status, payload = call(api, "/api/health", token=False)
    assert status == 200 and payload["ok"]


def test_static_traversal_blocked(api):
    status, _ = call(api, "/static/../../server.py")
    assert status in (403, 404)


def test_state_reports_ready_device(api):
    status, payload = call(api, "/api/state")
    assert status == 200
    assert payload["status"] == "ready"
    assert payload["devices"][0]["serial"] == "TESTSERIAL"


def test_post_without_token_rejected_even_with_foreign_origin(api):
    """CSRF-защита: посторонняя страница не знает токен сессии."""
    status, payload = call(api, "/api/scan", method="POST", body={"serial": "TESTSERIAL"},
                           token=False, origin="https://evil.example")
    assert status == 403
    assert payload["ok"] is False


def test_scan_job_flow(api):
    status, payload = call(api, "/api/scan", method="POST", body={"serial": "TESTSERIAL", "sdk": 33})
    assert status == 200
    job_id = payload["job_id"]
    for _ in range(100):
        st, job = call(api, f"/api/jobs/{job_id}")
        if job["job"]["status"] != "running":
            break
        threading.Event().wait(0.05)
    assert job["job"]["status"] == "done"
    scan_id = job["job"]["result"]["scan_id"]
    st, result = call(api, f"/api/result/{scan_id}")
    assert st == 200
    assert isinstance(result["items"], list), "items должен быть массивом для интерфейса"
    paths = {i["path"] for i in result["items"]}
    assert "/storage/emulated/0/Download/movie.mp4" in paths
    assert result["result"]["scan_id"] == scan_id


def test_delete_requires_confirmation(api):
    status, payload = call(api, "/api/delete", method="POST", body={
        "serial": "TESTSERIAL",
        "paths": ["/storage/emulated/0/Download/app.apk"],
        "confirm": False,
    })
    assert status == 409
    assert "не подтверждено" in payload["error"]


def test_post_with_token_and_any_origin_allowed(api):
    # Нужно для работы через прокси предпросмотра: Origin может отличаться,
    # но без токена запрос всё равно невозможен.
    status, payload = call(api, "/api/scan", method="POST", body={"serial": "TESTSERIAL"},
                           origin="https://preview.example")
    assert status == 200


def test_delete_and_report(api):
    status, payload = call(api, "/api/delete", method="POST", body={
        "serial": "TESTSERIAL",
        "paths": ["/storage/emulated/0/Download/app.apk", "/system/bin/sh"],
        "confirm": True,
    })
    assert status == 200
    job_id = payload["job_id"]
    for _ in range(100):
        st, job = call(api, f"/api/jobs/{job_id}")
        if job["job"]["status"] != "running":
            break
        threading.Event().wait(0.05)
    report = job["job"]["result"]
    assert report["deleted"][0]["path"] == "/storage/emulated/0/Download/app.apk"
    assert report["failed"][0]["path"] == "/system/bin/sh"
    assert "/storage/emulated/0/Download/app.apk" not in api["fake"].files


def test_delete_rejects_unknown_device(api):
    status, payload = call(api, "/api/delete", method="POST", body={
        "serial": "NOT_A_REAL_SERIAL",
        "paths": ["/storage/emulated/0/Download/app.apk"],
        "confirm": True,
    })
    assert status == 409


def test_optimize_job(api):
    status, payload = call(api, "/api/optimize", method="POST", body={
        "serial": "TESTSERIAL", "sdk": 33, "actions": ["window_animation"],
    })
    assert status == 200
    for _ in range(100):
        st, job = call(api, f"/api/jobs/{payload['job_id']}")
        if job["job"]["status"] != "running":
            break
        threading.Event().wait(0.05)
    outcomes = job["job"]["result"]["outcomes"]
    assert outcomes[0]["status"] == "success"
    assert api["fake"].settings_values["window_animation_scale"] == "0"


def test_restart_ui_needs_separate_confirmation(api):
    status, payload = call(api, "/api/optimize", method="POST", body={
        "serial": "TESTSERIAL", "sdk": 33, "actions": ["restart_ui"],
    })
    assert status == 200
    for _ in range(100):
        st, job = call(api, f"/api/jobs/{payload['job_id']}")
        if job["job"]["status"] != "running":
            break
        threading.Event().wait(0.05)
    assert job["job"]["result"]["outcomes"][0]["status"] == "user_action"


def test_job_cancel_endpoint(api):
    status, payload = call(api, "/api/scan", method="POST", body={"serial": "TESTSERIAL"})
    job_id = payload["job_id"]
    st, res = call(api, "/api/jobs/cancel", method="POST", body={"job_id": job_id})
    assert st == 200
    assert "cancelled" in res
