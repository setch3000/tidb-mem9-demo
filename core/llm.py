"""Claude 호출 (LangChain ChatAnthropic), Structured Output, 모델 설정.

모든 모델 호출은 이 파일을 거친다. ChatAnthropic이 지원하지 않는 기능이
스모크 테스트에서 확인되면, 해당 호출만 이 파일 안에서 anthropic SDK로 바꾼다.
"""
from __future__ import annotations

import json
import re

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from core.config import get_settings
from core.schemas import Translations

HANGUL = re.compile(r"[가-힣]")
CJK = re.compile(r"[가-힣一-鿿぀-ヿ]")
TRANSLATE_SYSTEM = {
    "ko": """주어진 JSON 배열의 문장을 자연스러운 한국어로 옮깁니다.
- 입력과 같은 순서, 같은 개수로 돌려줍니다.
- "User", "The user"는 "사용자"로 옮기고, 금융 용어는 한국에서 쓰는 표현으로 씁니다 (impermanent loss → 비영구적 손실).
- 프로토콜·거래소·자산 이름(Aave v3, Binance, USDC 등)은 그대로 둡니다.
- 내용을 더하거나 빼지 않습니다.""",
    "en": """Translate each sentence in the given JSON array into natural English.
- Return the same number of items in the same order.
- Use standard DeFi and finance terms (비영구적 손실 → impermanent loss, 예치 → deposit, 렌딩 → lending).
- Keep protocol, exchange, and asset names as they are (Aave v3, Binance, USDC, and so on).
- Do not add or remove content.""",
}


def needs_translation(text: str, target: str) -> bool:
    if not text:
        return False
    return not HANGUL.search(text) if target == "ko" else bool(CJK.search(text))


def get_llm(**overrides) -> ChatAnthropic:
    params = {"model": get_settings().llm_model, "max_tokens": 4096, **overrides}
    return ChatAnthropic(**params)


def structured(schema, *, include_raw: bool = False, **overrides):
    return get_llm(**overrides).with_structured_output(
        schema, method="json_schema", include_raw=include_raw
    )


def translate(texts: list[str], target: str) -> list[str]:
    """화면 표시용: 대상 언어가 아닌 문장만 옮긴다 (ko: 한글이 없는 문장, en: 한글·한자가 있는 문장).

    저장된 원본(mem9 기억, 분석 결과 등)은 바꾸지 않는다.
    단순 번역이라 thinking을 끈다 (Haiku 5.5는 effort high 이하에서 허용).
    """
    need = list(dict.fromkeys(t for t in texts if needs_translation(t, target)))
    if not need:
        return texts
    out = structured(Translations, effort="low", thinking={"type": "disabled"}, max_tokens=8192).invoke(
        [SystemMessage(TRANSLATE_SYSTEM[target]), HumanMessage(json.dumps(need, ensure_ascii=False))])
    mapping = dict(zip(need, out.items)) if len(out.items) == len(need) else {}
    return [mapping.get(t, t) for t in texts]


def output_language_rule(lang: str) -> str:
    """생성 프롬프트 끝에 붙이는 출력 언어 지시."""
    if lang == "en":
        return ("\n\n출력 언어: 영어. 사용자에게 보이는 모든 문장(제목, 근거, 요약, 검색 문장 등)을 자연스러운 영어로 씁니다. "
                "프로토콜·거래소·자산 이름은 그대로 둡니다.")
    return ("\n\n출력 언어: 한국어. 사용자에게 보이는 모든 문장을 한글로 씁니다. "
            "한자나 중국어를 섞지 않습니다 (영문 고유명사와 기호는 그대로).")
