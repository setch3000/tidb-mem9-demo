"""Behind the scenes 화면용 호출 기록 (SQL, mem9). 작업 공간마다 따로 쌓는다.

DB 연결과 mem9 클라이언트는 서버 전체가 하나를 같이 쓰므로, 호출을 일으킨 작업 공간을
ContextVar로 알아낸다. app.py가 매 실행 시작에 현재 작업 공간을 넣고, LangGraph는 노드를
다른 스레드에서 돌릴 때도 이 값을 복사해 넘긴다. 작업 공간이 없는 호출(스크립트, 전체 초기화)은
누구의 화면에도 보이지 않는다.
"""
from __future__ import annotations

import threading
from collections import deque
from contextvars import ContextVar
from typing import Any

current_workspace: ContextVar[str | None] = ContextVar("current_workspace", default=None)


class CallLog:
    def __init__(self, maxlen: int = 200):
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self._entries: dict[str | None, deque[dict[str, Any]]] = {}

    def append(self, entry: dict[str, Any]) -> None:
        workspace = current_workspace.get()
        with self._lock:
            self._entries.setdefault(workspace, deque(maxlen=self._maxlen)).append(entry)

    def recent(self, workspace: str) -> list[dict[str, Any]]:
        """이 작업 공간의 기록 (최신순)."""
        with self._lock:
            return list(reversed(self._entries.get(workspace, ())))
