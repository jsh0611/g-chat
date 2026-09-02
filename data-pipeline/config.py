#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
프로젝트 전역 설정 — 여기만 고치면 된다.
====================================================================
기본 동작 = 동해시시설관리공단(dhsisul.org) '전체 사이트' 수집.
  · 아래 [스파이더] 설정이 홈페이지에서 시작해 내부 모든 페이지를 따라가며 수집한다.
  · [BOARDS]/[FACILITIES] 는 '그것만 수집'하라는 뜻이 아니라, 해당 항목의
    제목·날짜·게시판명 같은 메타데이터를 더 정확히 뽑기 위한 (선택) 보강 시드다.
    → 목록에 없어도 스파이더가 일반 페이지로 전부 수집한다.
"""

from pathlib import Path

# ── 사이트 ──────────────────────────────────────────────
BASE = "https://www.dhsisul.org"
SUB = BASE + "/pages/sub.htm"

# ── 크롤링 매너 ─────────────────────────────────────────
DELAY = 1.0          # 요청 사이 대기(초). 공공 서버 배려 — 낮추지 말 것 권장
TIMEOUT = 20
DOWNLOAD_ATTACHMENTS = True   # 첨부파일까지 받을지 (main --no-download 로 끌 수 있음)
FORCE_RECRAWL = False         # True 면 이미 수집한 글도 강제로 다시 받음 (증분 무시)

# ── [전체 사이트 수집] 스파이더 ─────────────────────────
#   내부(dhsisul.org) 링크는 계속 따라가며 수집, 외부는 URL만 기록.
FOLLOW_LINKS = True             # 전체 사이트 수집 ON/OFF
SEED_URLS = [BASE + "/pages/index.htm"]   # 시작점(메인). 여기서 모든 메뉴로 뻗어나감
MAX_SPIDER_PAGES = None         # None=제한 없음(전체). ※ 처음 테스트는 30 등으로 두고 확인 권장
INTERNAL_HOST = "dhsisul.org"   # 이 도메인(서브도메인 포함)이면 '내부'로 간주

# 제외할 URL(부분 문자열). 걸리면 방문하지 않는다.
#   · 로그인/비밀번호 페이지는 '비밀번호 입력칸'을 자동 감지해 별도로 건너뜀.
EXCLUDE_URL_PATTERNS = [
    "new1699251277",                              # 불편사항 신고
    "mode=write", "mode=reply", "mode=modify",    # 글쓰기/수정 폼(내용 아님)
    "login", "member", "join",                    # 로그인/회원 관련
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# ── 데이터 경로 (실행 시 자동 생성) ─────────────────────
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW_DIR = DATA / "raw"              # raw.db(원본 HTML) + files/(첨부) + manifest.json
RAW_HTML = RAW_DIR / "html"
RAW_FILES = RAW_DIR / "files"
PARSED_DIR = DATA / "parsed"        # documents.json
PROCESSED_DIR = DATA / "processed"  # *.txt + documents.jsonl

# ── [핵심 섹션 1] 관리시설안내 — 모든 시설 자동 수집 ────
# 아래 카테고리 nav 아래의 '모든 시설'을 메뉴에서 자동으로 찾아 수집한다(손으로 나열 안 함).
FACILITY_NAVS = [
    "new1701823163",  # 관광시설
    "new1701855774",  # 체육시설
    "new1701855784",  # 교통시설
    "new1701855792",  # 시설안내
]
FACILITY_DETAIL_NAV = "new1701823163"   # 시설 상세는 이 nav+idx 로 접근해야 시설명(h1)이 정확히 나옴

# ── [핵심 섹션 2] 열린공단 — 모든 게시판 (code 자동 감지) ─
# nav_code 만 지정하면 목록에서 code 를 자동으로 읽어 모든 글을 수집한다.
# 목록에 글(mode=view)이 없으면 게시판이 아니라고 보고 넘어감(스파이더가 일반 페이지로 처리).
MAX_POSTS_PER_BOARD = None    # 게시판당 글 수. None=전체, 숫자=그 개수만(테스트 땐 5 등)
BOARDS = [
    {"name": "공지사항",         "nav_code": "new1698109729"},
    {"name": "채용공고",         "nav_code": "new1698109736"},
    {"name": "채용알림",         "nav_code": "new1766564806"},
    {"name": "적극행정 사례게시판", "nav_code": "new1701322129"},
    {"name": "적극행정 제안게시판", "nav_code": "new1701322153"},
    {"name": "칭찬합시다",       "nav_code": "new1699251289"},
    {"name": "자유게시판",       "nav_code": "new1699251308"},
    {"name": "행사안내",         "nav_code": "new1699251516"},
    {"name": "홍보사항",         "nav_code": "new1699251533"},
    {"name": "시설서비스개선실적", "nav_code": "new1782795855"},
    {"name": "FAQ",             "nav_code": "new1699251315"},
    {"name": "학생봉사활동",     "nav_code": "new1698109767"},
    # 아래는 스파이더가 '비밀번호 입력칸(비밀글 위젯)'을 로그인 페이지로 오인해 건너뛰던
    # 게시판들. 공개 게시판이므로 여기에 등록해 _crawl_board 로 직접 수집한다.
    # (직원사랑방 new1698109788 은 로그인 필요로 제외)
    {"name": "공단제규정",       "nav_code": "new1701162464"},
    {"name": "경영공시게시판",   "nav_code": "new1701308616"},
    {"name": "윤리경영 게시판",   "nav_code": "new1698109588"},
    {"name": "성적서",           "nav_code": "new1698109536"},
    {"name": "임직원 친인척 현황", "nav_code": "new1701308643"},
    {"name": "사전정보공표",     "nav_code": "new1699249010"},
    {"name": "정보목록",         "nav_code": "new1699249026"},
    {"name": "인권경영 자료실",   "nav_code": "new1699510864"},
    {"name": "ESG경영 우수사례", "nav_code": "new1698109529"},
    {"name": "영상정보처리기기운영·관리방침", "nav_code": "new1701839875"},
]