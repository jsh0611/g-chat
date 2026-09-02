# 로컬 전용 파일 관리 대장

`.gitignore`로 저장소에는 올라가지 않지만 개발에 필요한 파일들의 목록입니다.
**로컬 파일을 추가/삭제하면 이 표를 함께 갱신하세요.** (새 환경 세팅 시 이 문서 기준으로 준비)

## 데이터·설정

| 파일 | 용도 | 확보 방법 |
|---|---|---|
| `.env` | Supabase·factchat 게이트웨이·임베딩·TourAPI 키 (필수 키 목록은 CLAUDE.md 참고) | 관리자에게 요청 |
| `documents.jsonl` | 크롤링 원본 데이터 3,212문서 (개인정보 포함이라 비공개) | 크롤러 재실행 또는 팀 공유 |
| `CLAUDE.md` | 내부 개발 가이드 (아키텍처·함정·명령어) | 팀 공유 |
| `HANDOVER.md` | 인수인계 문서 — Supabase 신규 구축 절차(스키마 SQL 포함)·계정 정리·검증 | 인계 시 직접 전달 |
| `PORTFOLIO.md` | 이력서 첨부용 프로젝트 발전 기록 (피드백→기술도입→해결 루프 서사) | 개인 보관 |
| `user-feedback/` | 사용자 피드백 원본 (스크린샷·txt) + 리포트 HTML | 팀 공유 |

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

## 테스트 스크립트 (전부 `node <파일>`로 실행, `.env` 필요)

| 파일 | 검증 내용 | 기준 |
|---|---|---|
| `test_e2e_rag.js` | 실서비스 동일 조건 종단(수영교실·캠핑장 7문항). 검색·프롬프트 변경 후 회귀 확인용 | 7/7 |
| `test_e2e_more.js` | 확장 종단: FAQ·시설 표·최신 공지·할루시네이션·멀티턴 | 10~11/11 |
| `test_feedback.js` | 피드백 반영: 인사 재작성 통과, 의도 분류, "문맥" 어휘·"원하시면" 미노출, 프롬프트 유출 방지, 거절 앵커링 방지 | 17/17 |
| `test_tour.js` | 관광 모드 종단: 코스 질문에 tour 문서 검색 + POI 기반 답변 | 4/4 |
| `test_tools.js` | 실시간 도구: TAGO 고속버스·열차 조회, 축제 조회, 교통 슬롯 추출, 되묻기 판정·후속 턴 복원, 시간표 기반 답변 종단 | 9/9 |

배포 전 최소 실행 조합: `test_feedback.js` + `test_e2e_rag.js` (프롬프트 변경 시), `test_tour.js` 추가 (검색·가중치 변경 시), `test_tools.js` 추가 (도구·의도 변경 시).

## 임시 스크립트 규칙

- 일회성 진단·실험 스크립트는 `*_tmp.js`로 명명 (자동 ignore됨) → **결론이 나면 바로 삭제**
- 결론은 CLAUDE.md 함정(Gotchas) 목록이나 커밋 메시지에 남기고, 스크립트 자체는 남기지 않는다

## 정리 이력

- 2026-08-19: `rag_pipeline.js`(구버전), `test_table_llm/fix.js`·`sample.txt`(종료된 표 실험), `test_connect.js`(더미 insert 위험), `diag_yoga_tmp.js`·`retest_q2_tmp.js`(일회성) 삭제
