from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING = "waiting_for_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


@dataclass
class Task:
    goal: str
    start_url: str = ""
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    revision: int = 1
    status: TaskStatus = TaskStatus.QUEUED
    steps: int = 0
    elapsed: float = 0
    progress: str = "Đang chuẩn bị."
    checkpoint: str = ""
    result: str = ""
    error: str = ""

    def summary(self) -> dict:
        return {
            "task_id": self.id, "revision": self.revision, "goal": self.goal,
            "start_url": self.start_url,
            "status": self.status.value, "steps": self.steps,
            "progress": self.progress, "result": self.result, "error": self.error,
        }


class InputBlocked(RuntimeError):
    """The owner must explicitly resume after input is blocked."""


class InputGate:
    """Thread-safe revision check, shared by the worker and the input thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._revision = 0
        self._reason = "Chưa có task."

    def block(self, reason: str) -> None:
        with self._lock:
            self._reason = reason

    def open(self, revision: int) -> None:
        with self._lock:
            self._revision = revision
            self._reason = ""

    def check(self, revision: int) -> None:
        with self._lock:
            if self._reason or revision != self._revision:
                raise InputBlocked(self._reason or "Yêu cầu đã thay đổi; bỏ thao tác cũ.")
