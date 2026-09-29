from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Optional

from src.models.api_models import Info, Status1


@dataclass
class TaskState:
    status: Status1
    result: str = ""
    info: Optional[Info] = None


class TaskStore:
    """
    In-memory store mapping an opaque provisioning/unprovisioning task token to
    its current TaskState.

    In-memory (not Redis/database) because the adapter is stateless;
    task state is temporary and does not need to survive a
    restart. `/v1/provision` and `/v1/unprovision` share the same token
    namespace and the same `GET /v1/provision/{token}/status` endpoint (see
    docs/HLD.md).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskState] = {}

    def create_running_task(self) -> str:
        token = str(uuid.uuid4())
        with self._lock:
            self._tasks[token] = TaskState(status=Status1.RUNNING)
        return token

    def set_completed(self, token: str, result: str = "", info: Optional[Info] = None) -> None:
        with self._lock:
            self._tasks[token] = TaskState(status=Status1.COMPLETED, result=result, info=info)

    def set_failed(self, token: str, error_message: str) -> None:
        with self._lock:
            self._tasks[token] = TaskState(status=Status1.FAILED, result=error_message)

    def get(self, token: str) -> Optional[TaskState]:
        with self._lock:
            return self._tasks.get(token)


# Process-wide singleton, consistent with the in-memory/single-replica design decision.
task_store = TaskStore()
