-- TiDB 스키마 (ARCHITECTURE.md 3절).
-- scripts/seed.py가 주석 줄을 뺀 뒤 세미콜론 단위로 나눠 실행한다.
-- LangGraph 체크포인트 테이블(lg_*)은 TiDBSaver.setup()이 만든다.
-- 데모 초기화를 단순하게 하려고 외래 키 대신 인덱스만 둔다.

-- 사용자 프로필
CREATE TABLE IF NOT EXISTS users (
  id             INT PRIMARY KEY,
  name           VARCHAR(64)  NOT NULL,
  name_en        VARCHAR(64)  NOT NULL,
  risk_profile   VARCHAR(16)  NOT NULL,          -- conservative / balanced / aggressive
  max_risk_level TINYINT      NOT NULL,          -- 1(낮음) ~ 5(높음)
  base_currency  VARCHAR(8)   NOT NULL DEFAULT 'KRW'
);

-- 거래소·프로토콜 접근 경로
CREATE TABLE IF NOT EXISTS exchanges (
  id     INT PRIMARY KEY,
  name   VARCHAR(64) NOT NULL,
  type   VARCHAR(8)  NOT NULL,                   -- CEX / DEX
  region VARCHAR(8)  NOT NULL                    -- KR(국내 거래소) / GLOBAL(해외 거래소) / ONCHAIN
);

-- 위치(chain) 컬럼: 온체인 네트워크(arbitrum 등) 또는 거래소 계정(binance 등)

CREATE TABLE IF NOT EXISTS exchange_chains (
  exchange_id INT         NOT NULL,
  chain       VARCHAR(32) NOT NULL,
  PRIMARY KEY (exchange_id, chain)
);

CREATE TABLE IF NOT EXISTS user_exchange_accounts (
  user_id     INT         NOT NULL,
  exchange_id INT         NOT NULL,
  status      VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
  PRIMARY KEY (user_id, exchange_id)
);

-- 토큰 마스터
CREATE TABLE IF NOT EXISTS assets (
  symbol   VARCHAR(16) NOT NULL,
  chain    VARCHAR(32) NOT NULL,
  category VARCHAR(16) NOT NULL,                 -- stable / major / alt
  PRIMARY KEY (symbol, chain)
);

-- 실시간 보유 자산
CREATE TABLE IF NOT EXISTS user_holdings (
  user_id      INT            NOT NULL,
  asset_symbol VARCHAR(16)    NOT NULL,
  chain        VARCHAR(32)    NOT NULL,
  amount       DECIMAL(24, 8) NOT NULL,
  usd_value    DECIMAL(16, 2) NOT NULL,
  updated_at   TIMESTAMP      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, asset_symbol, chain)
);

-- 해외 거래소 월말 잔고 이력 (해외 계좌 신고 기준 5억 원 대비 참고 정보 표시)
CREATE TABLE IF NOT EXISTS balance_snapshots (
  user_id      INT            NOT NULL,
  month_end    DATE           NOT NULL,
  exchange_id  INT            NOT NULL,
  asset_symbol VARCHAR(16)    NOT NULL,
  amount       DECIMAL(24, 8) NOT NULL,
  price_usd    DECIMAL(16, 4) NOT NULL,
  krw_value    DECIMAL(18, 0) NOT NULL,
  PRIMARY KEY (user_id, month_end, exchange_id, asset_symbol)
);

-- DeFi 프로토콜과 거래소 예치·스테이킹 상품 (이름 + 위치 단위)
CREATE TABLE IF NOT EXISTS protocols (
  id         INT PRIMARY KEY,
  name       VARCHAR(64)    NOT NULL,
  chain      VARCHAR(32)    NOT NULL,
  tvl_usd    DECIMAL(20, 2),
  audited    BOOLEAN        NOT NULL,
  risk_score TINYINT        NOT NULL,            -- 1(낮음) ~ 5(높음)
  UNIQUE KEY uk_protocol (name, chain)
);

-- 실행 엔진이 이해하는 전략 템플릿
CREATE TABLE IF NOT EXISTS strategies (
  id              INT PRIMARY KEY,
  type            VARCHAR(16) NOT NULL,          -- swap / lend / stake / lp
  description     VARCHAR(255) NOT NULL,
  action_template JSON NOT NULL
);

-- 수집 원문 (합성 시드)
CREATE TABLE IF NOT EXISTS raw_signals (
  id        INT PRIMARY KEY,
  source    VARCHAR(32)   NOT NULL,              -- x / telegram / blog / youtube / announcement
  author    VARCHAR(64)   NOT NULL,
  content   VARCHAR(2000) NOT NULL,
  posted_at DATETIME      NOT NULL,
  analyzed  BOOLEAN       NOT NULL DEFAULT FALSE
);

-- LLM이 구조화한 투자 기회 (summary를 DB가 자동 임베딩)
CREATE TABLE IF NOT EXISTS opportunities (
  id           BIGINT PRIMARY KEY AUTO_RANDOM,
  signal_id    INT          NOT NULL,
  title        VARCHAR(128) NOT NULL,
  asset        VARCHAR(16)  NOT NULL,
  chain        VARCHAR(32)  NOT NULL,
  protocol_id  INT          NOT NULL,
  strategy_id  INT          NOT NULL,
  action_type  VARCHAR(16)  NOT NULL,
  expected_apy DECIMAL(6, 2),
  risk_level   TINYINT      NOT NULL,
  horizon      VARCHAR(16)  NOT NULL,
  confidence   DECIMAL(3, 2) NOT NULL,
  summary      VARCHAR(1500) NOT NULL,
  rationale    TEXT,
  status       VARCHAR(16)  NOT NULL DEFAULT 'ACTIVE',
  created_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  summary_vec  VECTOR(1024) GENERATED ALWAYS AS (EMBED_TEXT(
                 "tidbcloud_free/cohere/embed-multilingual-v3",
                 summary,
                 '{"input_type": "search_document", "input_type@search": "search_query"}'
               )) STORED,
  KEY idx_opp_signal (signal_id),
  VECTOR INDEX idx_opp_vec ((VEC_COSINE_DISTANCE(summary_vec)))
);

-- 추천 이력 (LangGraph 스레드와 연결)
CREATE TABLE IF NOT EXISTS recommendations (
  id             BIGINT PRIMARY KEY AUTO_RANDOM,
  user_id        INT          NOT NULL,
  opportunity_id BIGINT       NOT NULL,
  thread_id      VARCHAR(64)  NOT NULL,
  `rank`         TINYINT      NOT NULL,
  rationale      TEXT         NOT NULL,
  memory_refs    JSON,
  command        JSON,                           -- ExecutionCommand (승인 화면 미리보기)
  model          VARCHAR(64)  NOT NULL,
  status         VARCHAR(24)  NOT NULL DEFAULT 'AWAITING_APPROVAL',  -- / APPROVED / REJECTED
  decided_at     TIMESTAMP    NULL,
  created_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  KEY idx_rec_user_status (user_id, status),
  KEY idx_rec_thread (thread_id)
);

-- 실행 명령과 상태 (Mock 엔진)
CREATE TABLE IF NOT EXISTS execution_orders (
  id                BIGINT PRIMARY KEY AUTO_RANDOM,
  recommendation_id BIGINT      NOT NULL,
  command           JSON        NOT NULL,
  status            VARCHAR(16) NOT NULL DEFAULT 'PENDING',  -- / SUBMITTED / CONFIRMED / FAILED
  tx_hash           VARCHAR(80),
  created_at        TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
  updated_at        TIMESTAMP   DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_order_rec (recommendation_id)
);

-- 접속·사용 기록 (로그인, LLM 사용). 데모 초기화로 지우지 않는다 (scripts/seed.py APP_TABLES에 넣지 않음)
CREATE TABLE IF NOT EXISTS access_logs (
  id            BIGINT PRIMARY KEY AUTO_RANDOM,
  at            TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
  event         VARCHAR(32)   NOT NULL,          -- visit / login / logout / llm:* / admin:*
  email         VARCHAR(255),
  name          VARCHAR(255),
  ip            VARCHAR(64),                     -- 비워 둠: 현재 호스팅은 실제 접속자 IP를 전달하지 않음
  forwarded_for VARCHAR(255),                    -- 비워 둠: IP를 전달하는 호스팅으로 옮기면 사용
  user_agent    VARCHAR(512),
  session_id    VARCHAR(64),
  detail        JSON,
  KEY idx_access_at (at),
  KEY idx_access_email_at (email, at)
);
