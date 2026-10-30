"""하이라이트 장면(ARCHITECTURE.md 4.4)을 UI 없이 처음부터 끝까지 실행해 확인한다.

    python scripts/run_scenario.py                  # 초기화 → 분석 → 1회차 → 거절 → 2회차 → 승인·실행

시나리오 전용 작업 공간(scenario@localhost)에서 실행하므로 다른 방문자의 데이터에는 영향이 없다.
    python scripts/run_scenario.py --skip-reset     # 현재 데이터·기억 그대로 1회차부터
    python scripts/run_scenario.py --user 2
    python scripts/run_scenario.py --lang en       # 영어로 생성 (거절 사유도 영어)

흐름: 추천 → 추천 선택(interrupt) → 실행 agent가 execute_order tool 호출
      → tool 안에서 주문 승인 대기(interrupt) → 거절 또는 승인

합격 기준
  1) 1회차 추천에 LP가 있다
  2) 실행 agent가 execute_order tool을 호출해 승인 대기에 들어간다
  3) 거절 사유가 mem9 기억(insight 또는 대체 경로)으로 남는다
  4) 2회차 추천에 거절한 기회와 변동성 LP(리스크 3 이상)가 없다 (스테이블 LP는 허용)
  5) 2회차 근거에 기억이 인용된다 (memory_refs가 비어 있지 않다)
  6) 승인 후 주문이 CONFIRMED가 된다
  7) 승인 화면에 보인 명령과 실행·기록된 명령이 같다 (tool 재실행에도 바뀌지 않음)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.types import Command  # noqa: E402

from core import workflow  # noqa: E402
from core.db import get_db  # noqa: E402
from core.workspace import ensure_workspace, reset_workspace, workspace_id  # noqa: E402

WORKSPACE = workspace_id("scenario@localhost")

APPROVE_REASON = {
    "ko": "언제든 인출할 수 있는 안정적인 이자 수익이 좋아요.",
    "en": "I like steady interest that I can withdraw anytime.",
}
REJECT_REASON = {
    "ko": "변동성이 큰 상품은 부담스러워요. 비영구적 손실도 걱정돼요.",
    "en": "Volatile products make me uneasy. I am also worried about impermanent loss.",
}


def pending(state: dict) -> dict | None:
    """그래프가 멈춰 있으면 interrupt 값을 돌려준다."""
    return state["__interrupt__"][0].value if state.get("__interrupt__") else None


def recommend(user_id: int, lang: str) -> tuple[str, dict]:
    graph = workflow.get_recommend_graph()
    thread_id = workflow.new_thread_id(WORKSPACE)
    start = time.perf_counter()
    state = graph.invoke({"workspace": WORKSPACE, "user_id": user_id, "thread_id": thread_id, "lang": lang},
                         workflow.thread_config(thread_id))
    print(f"  ({time.perf_counter() - start:.1f}초, thread={thread_id})")
    print(f"  기억 {len(state['memories'])}개: " + " / ".join(m["content"] for m in state["memories"]))
    print(f"  검색 의도: {state['intent']}")
    if state.get("error"):
        print(f"  ✗ {state['error']}")
        return thread_id, state
    for c in state["cards"]:
        refs = " / ".join(m["content"] for m in c["memory_refs"]) or "-"
        print(f"  #{c['rank']} {c['headline']}  [{c['action_type']}, 리스크 {c['risk_level']}]")
        print(f"     {c['rationale']}")
        print(f"     참고한 기억: {refs}")
    return thread_id, state


def request_execution(thread_id: str, card: dict) -> dict | None:
    """추천을 고르면 실행 agent가 tool을 호출하고, tool 안의 승인 대기에서 멈춘다."""
    start = time.perf_counter()
    state = workflow.get_recommend_graph().invoke(
        Command(resume={"recommendation_id": card["recommendation_id"]}), workflow.thread_config(thread_id))
    print(f"  ({time.perf_counter() - start:.1f}초)")
    calls = [tc for m in state.get("messages", []) for tc in getattr(m, "tool_calls", None) or []]
    for tc in calls:
        print(f"  agent → tool {tc['name']}(note={tc['args'].get('note')!r}, 단계 {len(tc['args'].get('steps', []))}개)")
    approval = pending(state)
    if not approval or approval.get("type") != "order_approval":
        print(f"  ✗ 승인 대기에 들어가지 않았습니다: {state.get('outcome')} {state['messages'][-1].content}")
        return None
    sim = approval["simulation"]
    print(f"  승인 화면: {approval['note']}")
    for s in approval["command"]["steps"]:
        print(f"     {s['action']} {s['amount']:g} {s['from_asset']}→{s.get('to_asset') or '-'} @ {s['exchange']} "
              f"({s['chain']}, 슬리피지 {s['slippage_bps']}bps)")
    print(f"     시뮬레이션: 포지션 ${sim['position_usd']}, 가스 ${sim['gas_usd']}, 연 예상 ${sim['expected_yearly_usd']}")
    return approval


def decide(thread_id: str, decision: dict) -> dict:
    start = time.perf_counter()
    state = workflow.get_recommend_graph().invoke(Command(resume=decision), workflow.thread_config(thread_id))
    print(f"  ({time.perf_counter() - start:.1f}초)")
    for e in state.get("memory_events", []):
        print(f"  mem9 {e['kind']}: {e['content']}")
    print(f"  agent: {state['messages'][-1].content}")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", type=int, default=1)
    parser.add_argument("--skip-reset", action="store_true")
    parser.add_argument("--lang", choices=["ko", "en"], default="ko")
    args = parser.parse_args()
    checks: dict[str, bool] = {}

    ensure_workspace(WORKSPACE)
    if not args.skip_reset:
        print("[0] 데모 초기화")
        t = time.perf_counter()
        reset_workspace(WORKSPACE)
        print(f"  ({time.perf_counter() - t:.1f}초)")
        print("[1] 원문 분석 (analysis_graph)")
        t = time.perf_counter()
        result = workflow.run_analysis(WORKSPACE)
        print(f"  ({time.perf_counter() - t:.1f}초) 원문 {len(result.get('signals', []))}건 → 기회 {result.get('saved', 0)}개, "
              f"제외 {len(result.get('skipped', []))}건")

    print(f"\n[2] 1회차 추천 (사용자 {args.user}, 언어 {args.lang})")
    thread1, s1 = recommend(args.user, args.lang)
    if s1.get("error"):
        return 1
    lp = next((c for c in s1["cards"] if c["action_type"] == "lp"), None)
    checks["1회차에 LP 추천"] = lp is not None
    target = lp or s1["cards"][0]

    print(f"\n[3] 실행 요청: '{target['title']}' → 실행 agent")
    approval = request_execution(thread1, target)
    checks["agent가 execute_order 호출 → 승인 대기"] = approval is not None
    if approval is None:
        return 1

    print(f"\n[4] 주문 거절 / 사유: {REJECT_REASON[args.lang]}")
    s1 = decide(thread1, {"action": "reject", "reason": REJECT_REASON[args.lang]})
    checks["거절 사유가 기억으로 남음"] = any(e["kind"] in ("insight", "fallback") for e in s1.get("memory_events", []))

    print("\n[5] 2회차 추천 (새 스레드)")
    thread2, s2 = recommend(args.user, args.lang)
    if s2.get("error"):
        return 1
    checks["2회차에 거절한 기회·변동성 LP 없음"] = all(
        c["opportunity_id"] != target["opportunity_id"]
        and not (c["action_type"] == "lp" and c["risk_level"] >= 3)
        for c in s2["cards"])
    checks["2회차 근거에 기억 인용"] = any(c["memory_refs"] for c in s2["cards"])

    top = s2["cards"][0]
    print(f"\n[6] 실행 요청: '{top['title']}' → 승인")
    approval = request_execution(thread2, top)
    if approval is None:
        return 1
    s2 = decide(thread2, {"action": "approve", "reason": APPROVE_REASON[args.lang]})
    learned = [e for e in s2.get("memory_events", []) if e["kind"] in ("insight", "fallback")]
    print(f"  (참고) 승인 이유에서 배운 기억 {len(learned)}개")
    order = s2.get("order", {})
    print(f"  주문 {order.get('order_id')} → {order.get('status')} tx={str(order.get('tx_hash'))[:18]}…")
    checks["승인 후 CONFIRMED"] = order.get("status") == "CONFIRMED"

    executed = get_db().query("SELECT command FROM execution_orders WHERE id = %s", (order.get("order_id"),))
    executed_cmd = json.loads(executed[0]["command"]) if executed else {}
    checks["승인 화면 명령 = 실행 명령"] = (
        approval["command"]["command_id"] == s2["command"]["command_id"] == executed_cmd.get("command_id")
        and approval["command"]["steps"] == executed_cmd.get("steps"))

    print("\n합격 기준")
    for name, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
