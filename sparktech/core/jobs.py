"""Фоновые задания: прогресс, отмена, результаты.

Интерфейс не должен ждать длинных операций. Каждая операция выполняется в
отдельном потоке, публикует события прогресса через очередь и может быть
отменена. Результаты хранятся только в памяти процесса и исчезают при
закрытии SparkTech.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Job:
    def __init__(self, kind: str, meta: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.meta = meta or {}
        self.status = "running"  # running | done | error | cancelled
        self.error: str | None = None
        self.result: Any = None
        self.started_at = _now()
        self.finished_at: str | None = None
        self.events: deque[dict] = deque(maxlen=200)
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cv = threading.Condition(self._lock)

    # ---------------------------------------------------------------- события

    def push(self, stage: str, data: dict | None = None) -> None:
        with self._cv:
            self.events.append({"stage": stage, "time": _now(), **(data or {})})
            self._cv.notify_all()

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            events = list(self.events)[since:]
            payload = {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "error": self.error,
                "meta": self.meta,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "event_index": since + len(events),
                "events": events,
                "has_result": self.result is not None,
                "result": self.result,
            }
        return payload

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def cancel(self) -> bool:
        if self.finished:
            return False
        self.cancel_event.set()
        self.push("cancel", {"message": "Запрошена отмена операции"})
        return True

    # ---------------------------------------------------------------- запуск

    def start(self, target: Callable[["Job"], Any]) -> "Job":
        def _runner() -> None:
            try:
                result = target(self)
                with self._cv:
                    if self.cancel_event.is_set() and self.status == "running":
                        self.status = "cancelled"
                    elif self.status == "running":
                        self.status = "done"
                    self.result = result
                    self.finished_at = _now()
                    self._cv.notify_all()
            except Exception as exc:  # noqa: BLE001 — ошибка должна дойти до UI
                with self._cv:
                    self.status = "cancelled" if self.cancel_event.is_set() else "error"
                    self.error = str(exc) or exc.__class__.__name__
                    self.finished_at = _now()
                    self._cv.notify_all()
                self.push("error", {"message": self.error, "type": exc.__class__.__name__})

        self._thread = threading.Thread(target=_runner, name=f"job-{self.kind}-{self.id}", daemon=True)
        self._thread.start()
        return self

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def traceback_hint(self) -> str:
        return traceback.format_exc(limit=3)


class JobManager:
    def __init__(self, max_jobs: int = 20):
        self.max_jobs = max_jobs
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, target: Callable[[Job], Any], meta: dict | None = None) -> Job:
        job = Job(kind, meta)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self.max_jobs:
                oldest_id = next(iter(self._jobs))
                if oldest_id == job.id:
                    break
                del self._jobs[oldest_id]
        return job.start(target)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if not j.finished)
