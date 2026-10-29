"""환경변수 설정.

로컬에서는 프로젝트 루트의 .env를 읽고, Streamlit Community Cloud에서는
Secrets 화면의 최상위 키가 환경변수로 노출되므로 같은 코드로 동작한다.
ANTHROPIC_API_KEY, LANGSMITH_* 는 LangChain이 환경변수에서 직접 읽는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

REQUIRED = (
    "TIDB_HOST",
    "TIDB_USER",
    "TIDB_PASSWORD",
    "MEM9_API_KEY",
    "ANTHROPIC_API_KEY",
)


@dataclass(frozen=True)
class Settings:
    tidb_host: str
    tidb_port: int
    tidb_user: str
    tidb_password: str
    tidb_database: str
    mem9_api_url: str
    mem9_api_key: str
    llm_model: str


def missing(*names: str) -> list[str]:
    """값이 비어 있는 환경변수 이름 목록. 인자가 없으면 REQUIRED 전체를 확인한다."""
    return [n for n in (names or REQUIRED) if not os.environ.get(n)]


@lru_cache
def get_settings() -> Settings:
    env = os.environ
    return Settings(
        tidb_host=env.get("TIDB_HOST", ""),
        tidb_port=int(env.get("TIDB_PORT", "4000")),
        tidb_user=env.get("TIDB_USER", ""),
        tidb_password=env.get("TIDB_PASSWORD", ""),
        tidb_database=env.get("TIDB_DATABASE", "invest_demo"),
        mem9_api_url=env.get("MEM9_API_URL", "https://api.mem9.ai").rstrip("/"),
        mem9_api_key=env.get("MEM9_API_KEY", ""),
        llm_model=env.get("LLM_MODEL", "claude-haiku-5-5"),
    )
