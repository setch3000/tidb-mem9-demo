"""실행 엔진 시뮬레이션 (연동할 기존 자동화 엔진 자리).

연동할 엔진이 거래소 API 기반이든 온체인 기반이든 맞도록, 두 경로를 같은 API로 받는다.
주문 단계의 chain이 거래소 계정(binance 등)이면 거래소 API 경로, 네트워크(arbitrum 등)면 온체인 경로다.

엔진 API는 두 가지다.
- simulate(): 주문을 실제로 보내지 않고 예상 체결량·수수료·연 예상 수익을 계산한다.
- submit(): 주문을 PENDING → SUBMITTED → CONFIRMED로 진행하고 가짜 체결 ID(tx_hash)를 기록한다.
실행 agent의 execute_order tool이 이 API를 호출한다. 실제 연동 시 이 모듈만 바꾼다.
"""
from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.db import Database

HOLDINGS = Path(__file__).resolve().parent.parent / "data" / "holdings.json"
GAS_USD = {"ethereum": 4.5, "arbitrum": 0.15, "base": 0.05, "solana": 0.01, "bitcoin": 2.0, "kaia": 0.01}  # 합성 값
CEX_ACCOUNTS = {"upbit", "binance", "bybit", "okx", "coinbase", "bithumb", "coinone", "korbit"}


def route(chain: str) -> str:
    return "거래소 API" if chain.lower() in CEX_ACCOUNTS else "온체인"


@lru_cache
def prices() -> dict[str, float]:
    return json.loads(HOLDINGS.read_text(encoding="utf-8"))["prices"]


def simulate(steps: list[dict[str, Any]], expected_apy: float | None = None) -> dict[str, Any]:
    p = prices()
    rows, gas_total = [], 0.0
    for s in steps:
        from_price = p.get(s["from_asset"].upper())
        usd = s["amount"] * from_price if from_price else None
        received = None
        if s["action"] == "swap" and s.get("to_asset") and usd is not None and p.get(s["to_asset"].upper()):
            # 최악의 경우(슬리피지 한도 전부 소진)를 기준으로 계산
            received = usd / p[s["to_asset"].upper()] * (1 - s["slippage_bps"] / 10_000)
        # 거래소 계정 안 예치·스테이킹은 네트워크 수수료가 없다 (거래 수수료는 데모에서 생략)
        gas = 0.0 if route(s["chain"]) == "거래소 API" else GAS_USD.get(s["chain"].lower(), 1.0)
        gas_total += gas
        rows.append({
            "action": s["action"], "amount": s["amount"], "from_asset": s["from_asset"],
            "to_asset": s.get("to_asset"), "usd": round(usd, 2) if usd is not None else None,
            "min_received": round(received, 6) if received is not None else None, "gas_usd": gas,
            "route": route(s["chain"]),
        })
    # 포지션 = 첫 단계 입력 자산으로 들어가는 금액 합계 (LP는 스왑분 + 예치분)
    first = steps[0] if steps else None
    input_rows = [r for r, s in zip(rows, steps)
                  if first and s["from_asset"].upper() == first["from_asset"].upper() and r["usd"] is not None]
    position_usd = round(sum(r["usd"] for r in input_rows), 2) if input_rows else None
    return {
        "steps": rows,
        "routes": sorted({r["route"] for r in rows}),
        "gas_usd": round(gas_total, 2),
        "position_usd": position_usd,
        "expected_yearly_usd": round(position_usd * expected_apy / 100, 2)
        if position_usd is not None and expected_apy else None,
    }


def submit(db: Database, recommendation_id: int, command: dict[str, Any], *, step_delay: float = 0.6) -> dict:
    payload = json.dumps(command, ensure_ascii=False)
    order_id = db.execute(
        "INSERT INTO execution_orders (recommendation_id, command, status) VALUES (%s, %s, 'PENDING')",
        (recommendation_id, payload), label="실행: 주문 생성 (PENDING)")
    time.sleep(step_delay)
    db.execute("UPDATE execution_orders SET status = 'SUBMITTED' WHERE id = %s",
               (order_id,), label="실행: 엔진 전달 (SUBMITTED)")
    time.sleep(step_delay)
    tx_hash = "0x" + hashlib.sha256(f"{order_id}:{payload}".encode()).hexdigest()
    db.execute("UPDATE execution_orders SET status = 'CONFIRMED', tx_hash = %s WHERE id = %s",
               (tx_hash, order_id), label="실행: 체결 확인 (CONFIRMED)")
    return {"order_id": order_id, "status": "CONFIRMED", "tx_hash": tx_hash}
