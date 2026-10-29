"""해외 거래소 월말 잔고 정보 (해외 계좌 신고 참고 장면).

해외 거래소 월말 잔고 합계를 SQL로 집계해, 해외 계좌 신고 기준(월말 합계 5억 원)과의 거리를 보여준다.
계산은 모두 SQL로 하고 LLM은 관여하지 않는다. 화면 문구(ui.i18n "overseas.notice")는 사실 정보로만 두며,
신고 대상 여부 판단은 세무 전문가에게 맡긴다.
"""
from __future__ import annotations

from typing import Any

from core.db import Database

THRESHOLD_KRW = 500_000_000


def overseas_locations(db: Database) -> set[str]:
    """해외 거래소 계정 위치 이름 (예: binance, bybit). 주문 단계의 chain과 비교한다."""
    rows = db.query("SELECT LOWER(name) AS loc FROM exchanges WHERE region = 'GLOBAL'",
                    label="해외 잔고: 해외 거래소 목록")
    return {r["loc"] for r in rows}


def overseas_status(db: Database, user_id: int) -> dict[str, Any] | None:
    """최근 연도의 월별 해외 거래소 잔고 합계, 최고값, 기준까지 남은 금액. 해외 잔고가 없으면 None."""
    months = db.query(
        """
        SELECT s.month_end, SUM(s.krw_value) AS total_krw
        FROM balance_snapshots s
        JOIN exchanges e ON e.id = s.exchange_id AND e.region = 'GLOBAL'
        WHERE s.user_id = %s
          AND YEAR(s.month_end) = (SELECT MAX(YEAR(month_end)) FROM balance_snapshots WHERE user_id = %s)
        GROUP BY s.month_end
        ORDER BY s.month_end
        """, (user_id, user_id), label="해외 잔고: 월별 합계")
    if not months:
        return None
    latest = months[-1]
    by_exchange = db.query(
        """
        SELECT e.name AS exchange, s.asset_symbol, s.amount, s.krw_value
        FROM balance_snapshots s JOIN exchanges e ON e.id = s.exchange_id
        WHERE s.user_id = %s AND s.month_end = %s AND e.region = 'GLOBAL'
        ORDER BY s.krw_value DESC
        """, (user_id, latest["month_end"]), label="해외 잔고: 최근 월말 거래소별")
    peak = max(months, key=lambda m: m["total_krw"])
    return {
        "months": [{"month_end": m["month_end"].isoformat(), "total_krw": int(m["total_krw"])} for m in months],
        "latest_month": latest["month_end"].isoformat(),
        "latest_krw": int(latest["total_krw"]),
        "peak_month": peak["month_end"].isoformat(),
        "peak_krw": int(peak["total_krw"]),
        "threshold_krw": THRESHOLD_KRW,
        "gap_krw": THRESHOLD_KRW - int(peak["total_krw"]),
        "exceeded": int(peak["total_krw"]) > THRESHOLD_KRW,
        "by_exchange": [{**r, "amount": float(r["amount"]), "krw_value": int(r["krw_value"])} for r in by_exchange],
    }
