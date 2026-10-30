"""mem9 REST 클라이언트 (httpx), 호출 로그 기록.

스모크 테스트로 확인한 Hosted API 동작 (2026-10-29):
- q 검색은 의미 검색이 아니라 단어 일치에 가깝다 (한국어는 조사가 붙으면 못 찾음).
  → 추천 시에는 사용자(appId)의 기억 전체를 목록으로 가져와 LLM에 넘긴다.
- content 쓰기는 memory_type=pinned만 허용된다 (insight는 400).
- messages + mode=smart는 비동기(202)로 처리되고, 보통 10–25초 안에 insight가 추출된다.
  추출되지 않는 경우도 있어 호출부에서 대체 경로를 둔다.
- 추출된 insight는 입력이 한국어여도 영어로 저장된다.
"""
from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import httpx

from core.call_log import CallLog
from core.config import get_settings

MEMORIES = "/v1alpha2/mem9s/memories"


def user_app_id(user_id: int, workspace: str) -> str:
    """작업 공간 × 사용자별 기억 공간 (ARCHITECTURE.md 4.1)."""
    return f"{workspace}_user_{user_id}"


class Mem9Client:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 60):
        s = get_settings()
        self._http = httpx.Client(
            base_url=base_url or s.mem9_api_url,
            headers={"X-API-Key": api_key or s.mem9_api_key},
            timeout=timeout,
        )
        self.calls = CallLog(maxlen=200)   # Behind the scenes 화면용 호출 로그 (작업 공간별)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        start = time.perf_counter()
        res = self._http.request(method, path, **kwargs)
        self.calls.append({
            "method": method,
            "path": path,
            "params": kwargs.get("params"),
            "body": kwargs.get("json"),
            "status": res.status_code,
            "ms": round((time.perf_counter() - start) * 1000),
        })
        res.raise_for_status()
        return res.json() if res.content else None

    def status(self) -> str:
        return self._request("GET", "/v1alpha2/status").get("status", "")

    def list_memories(
        self,
        app_id: str,
        *,
        memory_type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        params: dict[str, Any] = {"appId": app_id, "limit": limit}
        if memory_type:
            params["memory_type"] = memory_type
        if tags:
            params["tags"] = ",".join(tags)
        return self._request("GET", MEMORIES, params=params).get("memories", [])

    def add_pinned(
        self, app_id: str, content: str, *, tags: list[str] | None = None, metadata: dict | None = None
    ) -> dict:
        body: dict[str, Any] = {"appId": app_id, "content": content, "memory_type": "pinned", "sync": True}
        if tags:
            body["tags"] = tags
        if metadata:
            body["metadata"] = metadata
        return self._request("POST", MEMORIES, json=body)

    def ingest(self, app_id: str, messages: list[dict[str, str]]) -> None:
        """대화를 넘겨 mem9가 사실을 추출·조정하게 한다 (비동기, 결과는 wait_for_insights로 확인)."""
        self._request("POST", MEMORIES, json={"appId": app_id, "mode": "smart", "messages": messages})

    def wait_for_insights(
        self, app_id: str, known_ids: set[str], *, timeout: float = 30, interval: float = 3
    ) -> list[dict]:
        """known_ids에 없던 insight가 생길 때까지 기다린다. 시간 안에 없으면 빈 목록."""
        deadline = time.monotonic() + timeout
        while True:
            new = [m for m in self.list_memories(app_id, memory_type="insight") if m["id"] not in known_ids]
            if new or time.monotonic() >= deadline:
                return new
            time.sleep(interval)

    def delete(self, memory_id: str) -> None:
        self._request("DELETE", f"{MEMORIES}/{memory_id}")

    def clear(self, app_id: str) -> int:
        """appId의 기억을 모두 지운다 (데모 초기화용)."""
        memories = self.list_memories(app_id, limit=200)
        for m in memories:
            self.delete(m["id"])
        return len(memories)

    def close(self) -> None:
        self._http.close()


@lru_cache
def get_mem9() -> Mem9Client:
    """프로세스 전체에서 공유하는 클라이언트 (호출 로그도 함께 쌓인다)."""
    return Mem9Client()
