"""화면 공통 도구: 사용자 선택, 표시용 번역, 스레드 상태 읽기, 추천 카드, 그래프 실행 스트리밍.

화면 상태는 브라우저 세션이 아니라 TiDB 체크포인트에서 다시 읽는다.
새로고침·재접속·다른 기기에서도 대기 중인 추천과 주문이 그대로 보인다.
"""
from __future__ import annotations

import hashlib
from typing import Any

import streamlit as st
from langchain_core.utils.json import parse_partial_json
from langgraph.types import Command

from core import engine_mock, workflow
from core.audit import log_event
from core.db import get_db
from core.llm import needs_translation, translate
from ui.i18n import LANGS, lang, t

PAGE_RECOMMEND = "app_pages/3_recommendations.py"
PAGE_APPROVAL = "app_pages/4_approval.py"
STEP_NODES = ("recall_memory", "build_intent", "search_candidates", "recommend", "validate",
              "save_recommendations", "exec_agent", "tools")
MEMORY_ICONS = {"pinned": "📌", "insight": "💡", "session": "💬"}


# ---- 사용자 ----

@st.cache_data(show_spinner=False, ttl=300)
def users() -> list[dict]:
    return get_db().query("SELECT id, name, name_en, risk_profile, max_risk_level FROM users ORDER BY id",
                          label="화면: 사용자 목록")


def persona_name(user: dict) -> str:
    return user["name"] if lang() == "ko" else user.get("name_en") or user["name"]


def current_user_id() -> int:
    return st.session_state.get("user_id", 1)


def current_user() -> dict:
    uid = current_user_id()
    return next((u for u in users() if u["id"] == uid), {"id": uid, "name": f"User {uid}", "name_en": f"User {uid}"})


# ---- 표시용 번역 ----

def _src_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@st.cache_data(show_spinner=False, max_entries=2000)
def _translate(texts: tuple[str, ...], target: str) -> list[str]:
    """TiDB 번역 캐시(translations)에서 먼저 찾고, 없는 문장만 Claude로 번역해 저장한다."""
    need = list(dict.fromkeys(x for x in texts if needs_translation(x, target)))
    if not need:
        return list(texts)
    db = get_db()
    hashes = {x: _src_hash(x) for x in need}
    rows = db.query(
        f"SELECT src_hash, text FROM translations WHERE lang = %s AND src_hash IN ({', '.join(['%s'] * len(need))})",
        (target, *hashes.values()), label="번역: 캐시 조회")
    cached = {r["src_hash"]: r["text"] for r in rows}
    mapping = {x: cached[h] for x, h in hashes.items() if h in cached}
    missing = [x for x in need if x not in mapping]
    if missing:
        done = translate(missing, target)
        new = [(hashes[x], target, x, y) for x, y in zip(missing, done) if y != x]
        if new:
            db.executemany(
                "INSERT INTO translations (src_hash, lang, src, text) VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE text = VALUES(text)", new, label="번역: 캐시 저장")
        mapping.update(zip(missing, done))
    return [mapping.get(x, x) for x in texts]


def localized(texts: list[str]) -> list[str]:
    """저장된 내용·다른 언어로 만든 결과를 현재 언어로 보여준다 (원본은 바꾸지 않는다)."""
    if not texts:
        return texts
    try:
        with st.spinner(t("common.translating")):   # 캐시에 있으면 0.5초 안에 끝나 표시되지 않는다
            return _translate(tuple(texts), lang())
    except Exception:
        return texts      # 번역 실패 시 원문 그대로 보여준다


def prepare_translations(texts: list[str]) -> None:
    """두 언어의 번역을 미리 만들어 캐시에 넣는다 (화면을 처음 열 때 기다리지 않도록)."""
    for target in LANGS:
        try:
            _translate(tuple(texts), target)
        except Exception:
            pass          # 미리 만들지 못해도 화면에서 다시 시도한다


def localized_one(text: str | None) -> str:
    return localized([text])[0] if text else (text or "")


def memory_lines(memories: list[dict]) -> list[str]:
    shown = localized([m["content"] for m in memories])
    return [f"{MEMORY_ICONS.get(m.get('memory_type'), '•')} {text}" for m, text in zip(memories, shown)]


def localize_cards(cards: list[dict]) -> list[dict]:
    """카드의 제목·근거·기회 이름을 현재 언어로 (한 번의 번역 호출로 묶는다)."""
    texts = [x for c in cards for x in (c["headline"], c["rationale"], c["title"])]
    out = localized(texts)
    return [{**c, "headline": out[i * 3], "rationale": out[i * 3 + 1], "title": out[i * 3 + 2]}
            for i, c in enumerate(cards)]


# ---- 스레드 상태 ----

def thread_snapshot(thread_id: str):
    return workflow.get_recommend_graph().get_state(workflow.thread_config(thread_id))


def pending(snapshot) -> dict | None:
    """스레드가 사람을 기다리고 있으면 interrupt 값({"type": ...})을 돌려준다."""
    return snapshot.interrupts[0].value if snapshot and snapshot.interrupts else None


def latest_thread(user_id: int) -> str | None:
    rows = get_db().query(
        "SELECT thread_id FROM recommendations WHERE user_id = %s ORDER BY created_at DESC, `rank` LIMIT 1",
        (user_id,), label="화면: 최근 추천 스레드")
    return rows[0]["thread_id"] if rows else None


def waiting_threads(user_id: int, kind: str) -> list[tuple[str, Any, dict]]:
    """kind 타입(choose_card / order_approval)으로 멈춰 있는 스레드 목록 (최근 것부터)."""
    rows = get_db().query(
        """
        SELECT thread_id, MAX(created_at) AS created_at FROM recommendations
        WHERE user_id = %s AND status = 'AWAITING_APPROVAL'
        GROUP BY thread_id ORDER BY created_at DESC LIMIT 5
        """, (user_id,), label="화면: 대기 중인 스레드")
    found = []
    for r in rows:
        snap = thread_snapshot(r["thread_id"])
        value = pending(snap)
        if value and value.get("type") == kind:
            found.append((r["thread_id"], snap, value))
    return found


# ---- 추천 카드 ----

def card_meta(card: dict) -> str:
    apy = t("card.apy", apy=f"{card['expected_apy']:g}") if card.get("expected_apy") is not None else t("card.apy_none")
    where = t("card.account", loc=card["chain"]) if engine_mock.route(card["chain"]) == "거래소 API" else card["chain"]
    return f"{card['protocol']} · {where} · {t('action.' + card['action_type'])} · {apy} · {t('card.risk', r=card['risk_level'])}"


def render_card(card: dict, *, button_key: str | None = None) -> bool:
    """추천 카드를 그리고, 버튼을 누르면 True. card는 localize_cards()를 거친 값."""
    with st.container(border=True):
        st.markdown(f"**#{card['rank']} {card['headline']}**")
        st.caption(card_meta(card))
        st.write(card["rationale"])
        if card.get("memory_refs"):
            with st.expander(t("card.memories", n=len(card["memory_refs"]))):
                for line in memory_lines(card["memory_refs"]):
                    st.markdown(f"- {line}")
        with st.expander(t("card.steps")):
            st.dataframe(card["proposed_steps"], hide_index=True, alt=t("card.steps_alt", title=card["title"]))
        if button_key:
            return st.button(t("card.request"), key=button_key, type="primary")
    return False


# ---- 그래프 실행 (스트리밍) ----

def _blocks(chunk) -> list[dict]:
    content = chunk.content
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


def stream_graph(graph_input, thread_id: str, status, *, show_cards: bool = False) -> None:
    """추천 그래프를 실행하며 진행 단계, AI 검토 메모, 만들어지는 카드를 status 안에 보여준다."""
    graph = workflow.get_recommend_graph()
    memo_box = status.empty()
    cards_box = status.empty()
    memo, body = "", ""
    for mode, payload in graph.stream(graph_input, workflow.thread_config(thread_id),
                                      stream_mode=["updates", "messages"]):
        if mode == "updates":
            for node, update in payload.items():
                if node not in STEP_NODES:
                    continue
                status.write(f"✓ {t('step.' + node)}")
                if node == "build_intent" and update:
                    status.caption(t("stream.intent", x=update["intent"]))
                if node == "search_candidates" and update:
                    status.caption(t("stream.candidates", n=len(update["candidates"])))
                if node == "exec_agent" and update:
                    for call in getattr(update["messages"][-1], "tool_calls", None) or []:
                        status.code(f"{call['name']}(note={call['args'].get('note')!r}, "
                                    f"steps={len(call['args'].get('steps', []))})", language="python")
            continue
        chunk, meta = payload
        if meta.get("langgraph_node") != "recommend":
            continue
        for b in _blocks(chunk):
            if b.get("type") == "thinking" and b.get("thinking"):
                memo += b["thinking"]
                memo_box.caption(t("stream.memo", x=memo[-300:]))
            elif b.get("type") == "text" and b.get("text") and show_cards:
                body += b["text"]
                parsed = parse_partial_json(body) if body.lstrip().startswith("{") else None
                drafts = (parsed or {}).get("cards") or []
                lines = [f"**#{d.get('rank', '?')} {d.get('headline', '')}**  \n{d.get('rationale', '')}"
                         for d in drafts if d.get("headline")]
                if lines:
                    cards_box.markdown("\n\n".join(lines))


def start_recommendation(user_id: int) -> str:
    thread_id = workflow.new_thread_id()
    log_event("llm:recommend", {"persona": user_id, "thread_id": thread_id, "lang": lang()})
    with st.status(t("status.building"), expanded=True) as status:
        stream_graph({"user_id": user_id, "thread_id": thread_id, "lang": lang()}, thread_id, status,
                     show_cards=True)
        status.update(label=t("status.built"), state="complete", expanded=False)
    return thread_id


def request_execution(thread_id: str, recommendation_id: int) -> None:
    log_event("llm:execute_request", {"thread_id": thread_id, "recommendation_id": str(recommendation_id)})
    with st.status(t("status.calling_tool"), expanded=True) as status:
        stream_graph(Command(resume={"recommendation_id": recommendation_id}, update={"lang": lang()}), thread_id, status)
        status.update(label=t("status.waiting_approval"), state="complete", expanded=False)


def decide_order(thread_id: str, decision: dict) -> None:
    log_event("llm:decision", {"thread_id": thread_id, "action": decision["action"],
                               "reason": decision.get("reason", "")})
    label = t("status.executing") if decision["action"] == "approve" else t("status.remembering")
    with st.status(label, expanded=True) as status:
        stream_graph(Command(resume=decision, update={"lang": lang()}), thread_id, status)
        status.update(label=t("status.done"), state="complete", expanded=False)
