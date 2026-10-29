# CLAUDE.md

## 공개 저장소 규칙

- 이 저장소는 **public**이다. 고객사 이름, 영업·계약 정보, 규제 검토 메모, 내부 판단처럼 외부에 보이면 안 되는 내용은 코드·문서·데이터·커밋 메시지 어디에도 넣지 않는다.
- 고객 맥락이 필요한 내부 문서는 `internal/`(git 제외)에 둔다.
- push 전에 비밀 값(.env의 키·비밀번호)과 위 내용이 들어갔는지 `git grep`으로 확인한다.

## Git 커밋 규칙

- 커밋 메시지에 `Co-Authored-By: Claude ...` 같은 공동 작성자(trailer) 줄을 **넣지 않는다.**
- 작성자는 `sehyun <21984972+setch3000@users.noreply.github.com>`(GitHub 비공개용 주소). 이 Mac에는 전역 git 사용자 설정이 없으므로 `git -c user.name="sehyun" -c user.email="21984972+setch3000@users.noreply.github.com" commit ...`으로 지정한다.
- 단계별 브랜치에서 작업하고, 검증이 끝나면 `main`에 fast-forward 머지한다.
- `.env`, `.streamlit/secrets.toml`, `.venv/`, `internal/`은 커밋하지 않는다. `.agents/`, `.claude/`도 사용자 확인 전까지 커밋하지 않는다.

## 머지 전 검증

```bash
.venv/bin/python scripts/smoke_test.py            # 외부 서비스 연결·호환성
.venv/bin/python scripts/run_scenario.py --lang ko # 하이라이트 장면, 합격 기준 7개
.venv/bin/python scripts/run_scenario.py --lang en
.venv/bin/python scripts/ui_check.py              # 화면 6개 × 언어 2개, 로그인 차단, 관리자 권한
```

설계와 결정 사항은 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)를 기준으로 한다.
