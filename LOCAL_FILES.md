# 로컬 전용 파일 관리 대장

`.gitignore`로 저장소에는 올라가지 않지만 개발에 필요한 파일들의 목록입니다.
**로컬 파일을 추가/삭제하면 이 표를 함께 갱신하세요.** (새 환경 세팅 시 이 문서 기준으로 준비)

## 데이터·설정

| 파일 | 용도 | 확보 방법 |
|---|---|---|
| `.env` | Supabase·factchat 게이트웨이·임베딩·TourAPI 키 (필수 키 목록은 CLAUDE.md 참고) | 관리자에게 요청 |
| `documents.jsonl` | 크롤링 원본 데이터 3,212문서 (개인정보 포함이라 비공개) | 크롤러 재실행 또는 팀 공유 |
| `documents_camping.jsonl` | 관광지 통합예약시스템(campingkorea.or.kr) 수집 결과 — `scripts/ingest_camping.js` 입력 | `cd data-pipeline && python main.py camping` 후 `data/processed/`에서 복사 (`scripts/refresh_data.ps1`이 자동 처리) |
| `ingest_state.json` | 적재 상태 — 수집에서 안 보인 문서의 연속 미발견 횟수(2회 연속이면 삭제) | 적재 스크립트가 자동 생성·갱신. 지우면 미발견 횟수가 초기화됨 |
| `documents.jsonl.bak`, `documents_camping.jsonl.bak` | 직전 수집본 백업 (개인정보 포함) | `scripts/refresh_data.ps1`이 복사 직전에 자동 보관 |
| `doc_key_aliases.json` | 2026-10 문서 고정 키 이전용 옛 페이지 ID → 새 키 표 | 이관 때 1회 생성(생성 스크립트 `make_key_aliases.py`는 2026-11-05 삭제). 첫 적재 후엔 영향 없고, 파일이 없으면 빈 표로 동작 |
| `dedup_exclusions.json` | 중복 제거 적용 목록(고정 키 기준) — 적재 때 이 문서들은 건너뜀 | `python data-pipeline/dedup.py --apply` |
| `CLAUDE.md` | 내부 개발 가이드 (아키텍처·함정·명령어) | 팀 공유 |
| `HANDOVER.md` | 인수인계 문서 — Supabase 신규 구축 절차(스키마 SQL 포함)·계정 정리·검증 | 인계 시 직접 전달 |
| `PORTFOLIO.md` | 이력서 첨부용 프로젝트 발전 기록 (피드백→기술도입→해결 루프 서사) | 개인 보관 |
| `user-feedback/` | 사용자 피드백 원본 (스크린샷·txt) + 리포트 HTML | 팀 공유 |
| `data-pipeline/data/` | 크롤러 원본·중간 산출물(raw HTML, 파싱 JSON, documents.jsonl 등, 개인정보 포함이라 비공개) | `python main.py`로 재수집 또는 팀 공유 |

## 논문·발표·과제 자료 (`paper/`, `*.pptx`, `*.pdf`, `*.xlsx` — 전부 ignore)

| 파일 | 용도 |
|---|---|
| `paper/topic-memo.md` | 논문 주제·클레임·평가 설계 확정 메모 |
| `paper/manus_prompt.md` | 선행연구 조사용 Manus 프롬프트 |
| `paper/prior_works.csv` | 검증 완료된 선행연구 20편 (2025+, 메이저 학회) |
| `paper/prior_works_review.md` | 20편 검토 — 용도별 참고 가이드·필독 7편 |
| `public_facility_chatbot_prior_work_2025_2026.xlsx` | Manus 조사 원본 |
| `G-LAB-동해.pdf` | G-Lab 리빙랩 과제 계획서 |
| `RAG_sLLM_챗봇_논문세미나.pptx` | 논문 세미나 발표 (PPT 템플릿 원본) |
| `동해챗봇_개선사항_발표.pptx` | 피드백 개선사항 발표 자료 |

## 테스트 스크립트 (`tests/` 폴더, 저장소 루트에서 `node tests/<파일>`로 실행, `.env` 필요)

| 파일 | 검증 내용 | 기준 |
|---|---|---|
| `test_feedback.js` | 피드백 반영 종단(실제 index.ts를 로컬 Deno로 기동): 문의처 오안내·"○○에 문의" 떠넘기기 금지·상담원 말투(LLM 채점), 예약 링크(시설별 주소·버튼 카드 없음)·링크 밑줄 손상, 교통 조회·되묻기, 관광, 인사, 프롬프트 유출 방지, 멀티턴 | 15/15 |
| `test_camping.js` | 캠핑 환불·추암 입실·예약 링크 (`npm run test:camping`) | 3/3 |
| `test_triage.js` | 2026-11 시범운영 피드백 트리아지 회귀(fix/keep 그룹, 결과는 `logs/triage-*.json`) (`npm run test:triage`) | keep 전부 |

배포 전 최소 실행 조합: `npm test`(test_feedback.js — 교통·관광·인사·유출 방지 포함) + `npm run test:triage` (프롬프트·검색 변경 시), `npm run test:camping` 추가 (캠핑 적재·예약 링크 변경 시).

## 임시 스크립트 규칙

- 일회성 진단·실험 스크립트는 `*_tmp.js`로 명명 (자동 ignore됨) → **결론이 나면 바로 삭제**
- 결론은 CLAUDE.md 함정(Gotchas) 목록이나 커밋 메시지에 남기고, 스크립트 자체는 남기지 않는다

## 정리 이력

- 2026-08-19: `rag_pipeline.js`(구버전), `test_table_llm/fix.js`·`sample.txt`(종료된 표 실험), `test_connect.js`(더미 insert 위험), `diag_yoga_tmp.js`·`retest_q2_tmp.js`(일회성) 삭제
- 2026-11-05: `diag_*_tmp.js` 12개·`_src_count_tmp.js`(9월 예약 링크·검색 순위 진단, 결론 반영 완료), `backfill_chunk_hash.js`(증분 적재 전환용 청크 해시 1회 이관, 실행 완료), `data-pipeline/make_key_aliases.py`(문서 고정 키 1회 이관) 삭제
- 2026-11-05: 테스트 표에서 `test_e2e_rag.js`·`test_e2e_more.js`·`test_tour.js`·`test_tools.js` 제거 (로컬에 없음 — 이전에 삭제된 것으로 보임). 교통·관광 경로는 `test_feedback.js`가 일부 커버
- 2026-11-05: JS를 `lib/`(공용 모듈)·`scripts/`(실행)·`tests/`로 정리, 실행 명령은 `package.json` scripts(`npm run …`)로 등록
