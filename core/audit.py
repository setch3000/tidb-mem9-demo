"""로그인 신원, 접속 IP, 접속·사용 기록 (access_logs).

- 신원: st.login(Google OIDC)으로 로그인한 st.user. 로컬 테스트에서만 DEMO_AUTH=off로 우회하고
  DEMO_DEV_EMAIL을 신원으로 쓴다 (배포 Secrets에는 넣지 않는다).
- IP는 기록하지 않는다. Streamlit Community Cloud는 실제 접속자 IP를 앱에 전달하지 않는다
  (2026-10-29 확인: X-Forwarded-For에 내부망 10.x 주소만 옴). access_logs의 ip·forwarded_for 컬럼은
  IP를 전달하는 호스팅으로 옮길 때를 위해 남겨 두고 비워 둔다.
- 기록은 데모 초기화로 지우지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import streamlit as st

from core.db import get_db

logger = logging.getLogger(__name__)


def auth_bypassed() -> bool:
    return os.environ.get("DEMO_AUTH", "").lower() == "off"


def auth_configured() -> bool:
    try:
        return "auth" in st.secrets
    except Exception:
        return False


def is_logged_in() -> bool:
    return auth_bypassed() or bool(getattr(st.user, "is_logged_in", False))


def identity() -> dict[str, str | None]:
    if auth_bypassed():
        email = os.environ.get("DEMO_DEV_EMAIL", "dev@localhost")
        return {"email": email, "name": "local-dev"}
    if getattr(st.user, "is_logged_in", False):
        return {"email": st.user.get("email"), "name": st.user.get("name")}
    return {"email": None, "name": None}


def admin_emails() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_admin() -> bool:
    email = (identity()["email"] or "").lower()
    return bool(email) and email in admin_emails()


def session_id() -> str:
    if "_audit_session" not in st.session_state:
        st.session_state["_audit_session"] = uuid.uuid4().hex[:16]
    return st.session_state["_audit_session"]


def _fit(value: Any, size: int) -> str | None:
    """컬럼 길이에 맞춘 문자열 (예상 밖의 값이 와도 기록이 빠지지 않게)."""
    return None if value is None else str(value)[:size]


def log_event(event: str, detail: dict[str, Any] | None = None) -> None:
    """access_logs에 한 줄 남긴다. 실패해도 데모는 계속되고 경고 로그만 남긴다."""
    who = identity()
    try:
        agent = st.context.headers.get("User-Agent")
    except Exception:
        agent = None
    try:
        get_db().execute(
            """INSERT INTO access_logs (event, email, name, ip, forwarded_for, user_agent, session_id, detail)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (event, _fit(who["email"], 255), _fit(who["name"], 255), None, None,
             _fit(agent, 512), session_id(), json.dumps(detail or {}, ensure_ascii=False, default=str)),
            label="기록: 이벤트 저장")
    except Exception:
        # 기록 실패가 데모를 막지 않게 하되, 빠진 기록을 알 수 있도록 로그를 남긴다
        logger.warning("access_logs 기록 실패 (event=%s)", event, exc_info=True)


def log_once(event: str, detail: dict[str, Any] | None = None) -> None:
    """세션당 한 번만 기록한다 (visit, login)."""
    key = f"_audit_logged_{event}"
    if not st.session_state.get(key):
        st.session_state[key] = True
        log_event(event, detail)
