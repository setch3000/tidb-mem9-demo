"""LLM Structured Output용 Pydantic 모델 (ARCHITECTURE.md 5절).

LLM이 긴 ID(19자리 AUTO_RANDOM, mem9 UUID)를 옮겨 적다 틀리지 않도록
후보와 기억은 프롬프트에 붙인 번호로 고르게 하고, 앱이 실제 ID로 바꾼다.
숫자 범위(1~5, 0~1)는 스키마 제약 대신 앱에서 검증·보정한다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ActionType = Literal["swap", "lend", "stake", "lp"]


# ---- 분석 그래프 ----

class Opportunity(BaseModel):
    title: str                     # 한국어 25자 이내, 예: "Aave v3 아비트럼 USDC 예치"
    asset: str                     # 사용자가 넣는 자산, 예: "USDC" (LP는 스테이블 쪽)
    chain: str                     # 예: "arbitrum"
    protocol: str                  # 지원 프로토콜 목록의 이름, 예: "Aave v3"
    action_type: ActionType
    expected_apy: float | None
    risk_level: int                # 1(낮음) ~ 5(높음)
    horizon: Literal["short", "mid", "long"]
    confidence: float              # 0 ~ 1
    summary: str                   # 한국어 2~3문장, 임베딩용
    rationale: str


class SignalAnalysis(BaseModel):
    is_actionable: bool
    reason: str                    # 기회가 없을 때 이유 (경고, 논평 등)
    opportunities: list[Opportunity]


# ---- 추천 그래프 ----

class SearchIntent(BaseModel):
    intent: str                    # 벡터 검색 질의 문장 (한국어 30~60자)
    reason: str                    # 이 문장을 만든 이유 (Behind the scenes 표시용)


class ExecutionStep(BaseModel):
    action: Literal["swap", "deposit", "stake", "withdraw"]
    exchange: str
    chain: str
    from_asset: str
    to_asset: str | None
    amount: float
    slippage_bps: int


class CommandDraft(BaseModel):
    steps: list[ExecutionStep]
    max_slippage_bps: int


class RecommendationCard(BaseModel):
    candidate_no: int              # 프롬프트의 후보 번호
    rank: int
    headline: str                  # 카드 제목 한 줄
    rationale: str                 # 한국어 2~4문장, 참고한 기억을 자연스럽게 언급
    memory_nos: list[int]          # 프롬프트의 기억 번호 (M1 → 1)
    command: CommandDraft


class RecommendationSet(BaseModel):
    cards: list[RecommendationCard]


class ExtractedFacts(BaseModel):
    facts: list[str]               # mem9 추출 실패 시 대체 경로: 한국어 짧은 문장 1~3개


class Translations(BaseModel):
    items: list[str]               # 화면 표시용 한국어 번역 (입력과 같은 순서·개수)


# ---- 실행 엔진 계약 ----

class ExecutionCommand(BaseModel):
    """기존 자동화 엔진에 넘어가는 계약. LLM 초안(CommandDraft)에 앱이 식별 정보를 채운다."""
    command_id: str
    user_id: int
    strategy_id: int
    steps: list[ExecutionStep]
    max_slippage_bps: int
    expires_at: str                # ISO 8601
    requires_approval: bool = True
