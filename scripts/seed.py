"""시드 데이터 적재, mem9 초기 기억 기록, 데모 초기화.

    python scripts/seed.py              # 테이블 생성(없으면) → 데이터 초기화 → 시드 적재 → mem9 초기 기억
    python scripts/seed.py --recreate   # 앱 테이블을 지우고 다시 만든 뒤 위와 같이 진행 (스키마 변경 시)
    python scripts/seed.py --skip-mem9  # mem9은 건드리지 않음

앱의 사이드바 "데모 초기화" 버튼도 reset_demo()를 호출한다.
초기화 범위: 앱 테이블 전체, LangGraph 체크포인트(lg_*), mem9의 user_<id> 기억.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.checkpoint_tidb import TiDBSaver  # noqa: E402
from core.db import Database, get_db  # noqa: E402
from core.mem9_client import Mem9Client, user_app_id  # noqa: E402

DATA = ROOT / "data"
SCHEMA = ROOT / "sql" / "schema.sql"

# 삭제 순서 = 참조하는 쪽 먼저 (외래 키는 없지만 의미상 순서를 지킨다)
APP_TABLES = [
    "execution_orders", "recommendations", "opportunities", "raw_signals",
    "balance_snapshots", "user_holdings", "user_exchange_accounts", "exchange_chains",
    "strategies", "protocols", "assets", "exchanges", "users",
]
CHECKPOINT_TABLES = ["lg_checkpoint_writes", "lg_checkpoint_blobs", "lg_checkpoints"]


def load(name: str) -> Any:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def schema_statements() -> list[str]:
    # 주석 줄을 먼저 빼고 세미콜론으로 나눈다 (주석 안의 문자가 구분자로 읽히지 않도록)
    lines = [line for line in SCHEMA.read_text(encoding="utf-8").splitlines()
             if not line.strip().startswith("--")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def apply_schema(db: Database, *, recreate: bool = False) -> None:
    if recreate:
        for table in APP_TABLES:
            db.execute(f"DROP TABLE IF EXISTS {table}", label="seed: drop")
    for sql in schema_statements():
        db.execute(sql, label="seed: schema")
    TiDBSaver(db).setup()


def clear_data(db: Database) -> None:
    for table in APP_TABLES + CHECKPOINT_TABLES:
        db.execute(f"DELETE FROM {table}", label="seed: reset")


def insert_seed(db: Database) -> dict[str, int]:
    users = load("users.json")
    exchanges = load("exchanges.json")["exchanges"]
    holdings = load("holdings.json")
    catalog = load("protocols.json")
    signals = load("signals.json")
    prices = holdings["prices"]
    snap = holdings["overseas_snapshots"]

    rows = {
        "users": [
            (u["id"], u["name"], u["name_en"], u["risk_profile"], u["max_risk_level"], u["base_currency"]) for u in users
        ],
        "exchanges": [(e["id"], e["name"], e["type"], e["region"]) for e in exchanges],
        "exchange_chains": [(e["id"], chain) for e in exchanges for chain in e["chains"]],
        "user_exchange_accounts": [(u["id"], ex_id) for u in users for ex_id in u["exchange_ids"]],
        "assets": [(a["symbol"], a["chain"], a["category"]) for a in holdings["assets"]],
        "user_holdings": [
            (h["user_id"], h["asset_symbol"], h["chain"], h["amount"],
             round(h["amount"] * prices[h["asset_symbol"]], 2))
            for h in holdings["holdings"]
        ],
        "protocols": [
            (p["id"], p["name"], p["chain"], p["tvl_usd"], p["audited"], p["risk_score"])
            for p in catalog["protocols"]
        ],
        "strategies": [
            (s["id"], s["type"], s["description"], json.dumps(s["action_template"], ensure_ascii=False))
            for s in catalog["strategies"]
        ],
        "raw_signals": [
            (s["id"], s["source"], s["author"], s["content"], s["posted_at"]) for s in signals
        ],
        "balance_snapshots": [
            (b["user_id"], month, b["exchange_id"], b["asset_symbol"], amount,
             snap["month_prices"][b["asset_symbol"]][i],
             round(amount * snap["month_prices"][b["asset_symbol"]][i] * holdings["usd_krw"]))
            for b in snap["balances"]
            for i, (month, amount) in enumerate(zip(snap["months"], b["amounts"]))
        ],
    }
    columns = {
        "users": "id, name, name_en, risk_profile, max_risk_level, base_currency",
        "exchanges": "id, name, type, region",
        "exchange_chains": "exchange_id, chain",
        "user_exchange_accounts": "user_id, exchange_id",
        "assets": "symbol, chain, category",
        "user_holdings": "user_id, asset_symbol, chain, amount, usd_value",
        "protocols": "id, name, chain, tvl_usd, audited, risk_score",
        "strategies": "id, type, description, action_template",
        "raw_signals": "id, source, author, content, posted_at",
        "balance_snapshots": "user_id, month_end, exchange_id, asset_symbol, amount, price_usd, krw_value",
    }
    for table, values in rows.items():
        placeholders = ", ".join(["%s"] * len(values[0]))
        db.executemany(
            f"INSERT INTO {table} ({columns[table]}) VALUES ({placeholders})", values, label="seed: insert"
        )
    return {table: len(values) for table, values in rows.items()}


def seed_memories(mem9: Mem9Client) -> dict[str, int]:
    """사용자별 기억을 비우고 온보딩 선호를 pinned 기억으로 기록한다."""
    result = {}
    for user in load("users.json"):
        app_id = user_app_id(user["id"])
        mem9.clear(app_id)
        for content in user["onboarding_memories"]:
            mem9.add_pinned(app_id, content, tags=["preference"])
        result[app_id] = len(user["onboarding_memories"])
    return result


def reset_demo(
    db: Database | None = None,
    mem9: Mem9Client | None = None,
    *,
    recreate: bool = False,
    skip_mem9: bool = False,
) -> dict[str, Any]:
    db = db or get_db()
    apply_schema(db, recreate=recreate)
    clear_data(db)
    summary: dict[str, Any] = {"tables": insert_seed(db)}
    if not skip_mem9:
        own_client = mem9 is None
        mem9 = mem9 or Mem9Client()
        try:
            summary["memories"] = seed_memories(mem9)
        finally:
            if own_client:
                mem9.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="데모 시드 적재 / 초기화")
    parser.add_argument("--recreate", action="store_true", help="앱 테이블을 지우고 다시 생성")
    parser.add_argument("--skip-mem9", action="store_true", help="mem9 기억은 건드리지 않음")
    args = parser.parse_args()

    start = time.perf_counter()
    summary = reset_demo(recreate=args.recreate, skip_mem9=args.skip_mem9)
    for table, n in summary["tables"].items():
        print(f"  {table:<24} {n:>3}행")
    for app_id, n in summary.get("memories", {}).items():
        print(f"  mem9 {app_id:<19} {n:>3}개 (pinned)")
    print(f"완료 ({time.perf_counter() - start:.1f}초)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
