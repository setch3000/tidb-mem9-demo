"""방문자별 작업 공간: 분석 결과·추천·체크포인트·mem9 기억을 Google 계정마다 따로 둔다.

- 작업 공간 ID는 이메일의 SHA-256 앞 12자리다 (이메일 자체는 저장하지 않는다).
- 사용자 A·B·C의 프로필·보유 자산·계정·잔고 이력은 모든 작업 공간이 같은 시드를 쓴다 (주문은 Mock이라 바뀌지 않음).
- 처음 쓰는 작업 공간은 mem9에 온보딩 기억을 기록하고 시작한다. 초기화도 자기 작업 공간만 되돌린다.
"""
from __future__ import annotations

import hashlib
import json

from core.config import ROOT
from core.db import Database, get_db
from core.mem9_client import Mem9Client, get_mem9, user_app_id


def workspace_id(email: str) -> str:
    return "w" + hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:12]


def thread_prefix(workspace: str) -> str:
    """추천 스레드 ID 앞부분. 초기화할 때 이 작업 공간의 체크포인트를 찾는 데 쓴다."""
    return f"rec_{workspace}_"


def _personas() -> list[dict]:
    return json.loads((ROOT / "data" / "users.json").read_text(encoding="utf-8"))


def seed_memories(workspace: str, mem9: Mem9Client | None = None) -> dict[str, int]:
    """작업 공간의 사용자별 기억을 비우고 온보딩 선호를 pinned 기억으로 기록한다."""
    mem9 = mem9 or get_mem9()
    result = {}
    for user in _personas():
        app_id = user_app_id(user["id"], workspace)
        mem9.clear(app_id)
        for content in user["onboarding_memories"]:
            mem9.add_pinned(app_id, content, tags=["preference"])
        result[app_id] = len(user["onboarding_memories"])
    return result


def ensure_workspace(workspace: str, db: Database | None = None) -> bool:
    """처음 쓰는 작업 공간이면 mem9 기억을 준비한다. 새로 만들었으면 True."""
    db = db or get_db()
    if db.query("SELECT 1 FROM workspaces WHERE id = %s", (workspace,), label="작업 공간: 확인"):
        return False
    seed_memories(workspace)
    db.execute("INSERT IGNORE INTO workspaces (id) VALUES (%s)", (workspace,), label="작업 공간: 생성")
    return True


def clear_analysis(workspace: str, db: Database | None = None) -> None:
    """이 작업 공간의 분석 결과를 지우고 원문을 미분석 상태로 되돌린다."""
    db = db or get_db()
    db.execute("DELETE FROM opportunities WHERE workspace = %s", (workspace,), label="분석: 초기화")
    db.execute("DELETE FROM analyzed_signals WHERE workspace = %s", (workspace,), label="분석: 초기화")


def reset_workspace(workspace: str, db: Database | None = None) -> None:
    """이 작업 공간만 시작 상태로 되돌린다: 분석 결과, 추천·주문, 체크포인트, mem9 기억."""
    db = db or get_db()
    clear_analysis(workspace, db)
    db.execute(
        """DELETE e FROM execution_orders e JOIN recommendations r ON r.id = e.recommendation_id
           WHERE r.workspace = %s""", (workspace,), label="초기화: 주문")
    db.execute("DELETE FROM recommendations WHERE workspace = %s", (workspace,), label="초기화: 추천")
    pattern = thread_prefix(workspace).replace("_", r"\_") + "%"
    for table in ("lg_checkpoint_writes", "lg_checkpoint_blobs", "lg_checkpoints"):
        db.execute(f"DELETE FROM {table} WHERE thread_id LIKE %s", (pattern,), label="초기화: 체크포인트")
    seed_memories(workspace)
    db.execute(
        "INSERT INTO workspaces (id, reset_at) VALUES (%s, NOW()) ON DUPLICATE KEY UPDATE reset_at = NOW()",
        (workspace,), label="초기화: 작업 공간")
