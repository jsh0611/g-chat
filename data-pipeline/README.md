# data-pipeline

동해시시설관리공단(dhsisul.org) 홈페이지·문서 수집 → RAG 데이터 파이프라인.
최종 산출물 `data/processed/documents.jsonl`을 g-chat 저장소 루트의 `ingest.js`(Node/Supabase)가 읽어 청킹·임베딩·적재한다.

## 파이프라인 구조

```
data-pipeline/
├── main.py               # 실행기 (crawl → parse → preprocess)
├── config.py             # 수집 대상·제외·크롤링 매너 설정 (여기만 고치면 됨)
├── rawhtml.py            # 원본 HTML 저장소 (SQLite: data/raw/raw.db)
├── requirements.txt
│
├── crawler/              # 1단계: 수집 (유일하게 네트워크 사용)
│   └── crawler.py        #   게시판/시설 + 내부 전체 스파이더 + 증분
├── parser/               # 2단계: HTML → 구조화 JSON (오프라인)
│   └── parser.py         #   병합셀(rowspan/colspan) 표 파싱
├── preprocessor/         # 3단계: 정제 → txt + documents.jsonl (오프라인)
│   ├── preprocessor.py
│   ├── extractors.py     #   PDF(pdfplumber)·HWP(LibreOffice) 첨부 표 추출
│   ├── ocr.py             #   이미지·스캔 PDF OCR (Tesseract, 파이프라인과 분리 실행)
│   └── merge_ocr.py       #   OCR 결과를 documents.jsonl 에 병합
└── data/                 # 실행 시 자동 생성 (크롤링 원본 포함 — git에서 제외)
    ├── raw/              #   raw.db(원본 HTML) + files/(첨부) + manifest.json + external_links.json
    ├── parsed/           #   documents.json
    └── processed/        #   *.txt(사람용) + documents.jsonl(g-chat 입력)
```

## 설치 & 실행

```bash
cd data-pipeline
pip install -r requirements.txt      # requests, beautifulsoup4, pdfplumber

python main.py               # 전체: 수집 → 구조화 → 전처리
python main.py crawl         # 수집만 (증분)
python main.py crawl --reset # 처음부터 재수집
python main.py parse         # 구조화만
python main.py preprocess    # 전처리만 (txt/jsonl 재생성)
```

(선택) 첨부 텍스트 추출 확장:
- HWP(v5) 첨부: `pip install pyhwp`
- HWPX/DOC(X)/XLS(X)/PPT(X)/ODT 첨부: LibreOffice 설치 (Windows: libreoffice.org / Ubuntu: `apt install libreoffice`)
- 이미지·스캔 PDF의 OCR: Tesseract 설치 후 `python -m preprocessor.ocr` (본 파이프라인과 분리 실행, `merge_ocr.py`로 결과 병합)

미설치 도구가 필요한 첨부는 건너뛰고 나머지는 정상 동작한다.

※ 반드시 `data-pipeline/` 폴더 안에서 실행 (모듈 import 경로 때문).

## 주요 기능

- **전체 사이트 수집**: 메인 페이지(`SEED_URLS`)에서 시작해 내부(dhsisul.org) 링크를 재귀적으로 따라감.
  게시판·게시글·시설·안내 페이지 모두 수집.
- **제외**: 불편사항 신고·글쓰기 폼(`EXCLUDE_URL_PATTERNS`), 로그인/비밀번호 페이지(자동 감지)는 건너뜀.
- **증분 재실행**: 이미 수집한 글은 재요청하지 않고, 목록 페이지는 매 실행 다시 훑어 **새 글만** 추가.
  → 서버에 올려 주기 실행하면 새 글이 자동으로 쌓임.
- **표**: 페이지 내 HTML 표는 병합셀까지 격자로 펼쳐 추출. 첨부(PDF/HWP) 안의 표도 추출.
- **외부 사이트**: 방문하지 않고 URL만 `data/raw/external_links.json`에 기록(전용 크롤러 판단용).
- **원본 캐시**: HTML은 `raw.db`(SQLite) 한 파일에 저장 → 재파싱 시 사이트 재방문 불필요.

## 설정 (config.py)

- `BOARDS`: 풍부한 메타데이터(제목·날짜)로 수집할 게시판. `{name, nav_code, code}` 추가.
- `MAX_POSTS_PER_BOARD`: 게시판당 글 수. None=전체.
- `MAX_SPIDER_PAGES`: 전체 스파이더 상한. None=제한 없음. **테스트는 30 등으로 시작 권장**.
- `EXCLUDE_URL_PATTERNS`: 제외할 URL 부분 문자열.
- `DELAY`: 요청 간 대기(초). 공공 서버 배려 — 낮추지 말 것.

## 산출물 형식 (documents.jsonl)

라인당 1문서: `{"id", "content", "metadata": {source_type, title, url, date, source, ...}}`

- `content`: 임베딩·검색 대상 텍스트(제목+본문+표+첨부).
- `metadata`: RAG 답변의 출처(URL) 인용에 사용. 저장소 루트의 `ingest.js`가 이 파일을 읽어 청킹·임베딩·Supabase 적재.

## 이 사이트 특성 메모

- URL은 `sub.htm?nav_code=..&mv_data=<base64>||`. `mv_data`는 쿼리스트링을 base64 인코딩한 값.
- 게시판 상세(view)는 Referer 검사가 있어 목록 URL을 Referer로 넣어야 함.
- 본문 컨테이너는 `#sub-content`. (개편 시 parser 셀렉터만 수정)
