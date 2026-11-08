#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1단계 — 수집: 동해시 관광지 통합예약시스템(campingkorea.or.kr)  ※ 허용 목록 + 증분
====================================================================
dhsisul.org 크롤러(crawler.py)와 달리 링크를 재귀로 따라가지 않는다.
config.py 의 CK_* 목록에 적힌 URL 만 요청하고, 그 밖의 링크는 URL 만 기록한다.

  · 정적 페이지(이용안내·시설별 안내전화·캠핑장 소개 등·숙박/캠핑시설 상세)
      → 매 실행 다시 받는다(요금·규정이 바뀔 수 있고 합쳐서 40쪽 미만).
  · 게시판(1001 공지·1002 FAQ)
      → 목록 페이지네이션을 끝까지 따라가 과거 글까지 수집.
        재실행 시에는 이미 받은 글은 재요청하지 않고(증분), 새 글이 하나도 없는
        목록 쪽을 만나면 더 과거 쪽은 넘기지 않는다. (--reset 이면 처음부터)
  · 여행코스(1010) → 상세가 외부(visitkorea) 링크뿐이라 목록 카드 HTML 만 저장.

원본 HTML 은 dhsisul 과 같은 data/raw/raw.db 에 'ck_' 접두 키로, 첨부는
data/raw/files/campingkorea/ 에 q_fileId 기준으로 저장한다.
manifest 는 data/raw/camping_manifest.json (dhsisul manifest 와 분리).
"""

import json
import re
import sys
import time
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

import config as C
import rawhtml
from crawler.crawler import _cd_filename, _progress

MANIFEST = C.RAW_DIR / "camping_manifest.json"
STATE = C.RAW_DIR / "camping_state.json"      # 게시판별 '과거 글까지 끝까지 수집 완료' 여부
EXTERNAL = C.RAW_DIR / "camping_external_links.json"
FILES_DIR = C.RAW_FILES / "campingkorea"

# 이 사이트 전용 세션(헤더는 dhsisul 과 동일). 같은 세션으로 수백 건을 요청하면 어느 순간부터
# 그 세션(DHCMP_JSESSIONID)에만 모든 요청이 400 으로 돌아온다(2026-09 첫 전체 수집 때 약 280건째,
# 새 세션으로는 같은 URL 이 200). → 400 이면 쿠키를 버리고 CK_SESSION_RESET_WAIT 초 쉰 뒤 1회 재시도.
# 요청 간격은 CK_DELAY(2초) — config.py
session = requests.Session()
session.headers.update(C.HEADERS)


def _reset_session():
    session.cookies.clear()
    time.sleep(C.CK_SESSION_RESET_WAIT)


# ── 허용 목록 가드 ──────────────────────────────────────
def _denied(url: str) -> bool:
    return any(p in url for p in C.CK_DENY_PATTERNS)


def _get(url: str, referer: str, stream: bool = False):
    for attempt in (1, 2):
        time.sleep(C.CK_DELAY)
        r = session.get(url, headers={"Referer": referer}, timeout=C.TIMEOUT, stream=stream)
        if r.status_code == 400 and attempt == 1:
            _reset_session()
            continue
        r.raise_for_status()
        return r


def _fetch(url: str, referer: str | None = None) -> str:
    """모든 요청의 단일 관문. 금지 패턴·타 도메인이면 요청 자체를 하지 않는다."""
    if _denied(url) or urlparse(url).netloc != urlparse(C.CK_BASE).netloc:
        raise PermissionError(f"허용 목록 밖 URL 요청 차단: {url}")
    r = _get(url, referer or C.CK_BASE + "/index.do")
    r.encoding = "utf-8"
    return r.text


def _content(html: str):
    soup = BeautifulSoup(html, "html.parser")
    return soup, (soup.select_one(".contents") or soup.body or soup)


def _record_links(root, page_url: str, external: dict):
    """본문의 외부 링크는 방문하지 않고 URL 만 기록 (dhsisul 크롤러와 같은 방침)."""
    for a in root.find_all("a", href=True):
        href = a["href"].strip()
        m = re.match(r"javascript:openUrl\('([^']+)'\)", href)
        if m:
            href = m.group(1)
        if not href.startswith(("http://", "https://")):
            continue
        if urlparse(href).netloc != urlparse(C.CK_BASE).netloc:
            external.setdefault(href, page_url)


# ── 첨부 ────────────────────────────────────────────────
def _attachments(soup, page_url: str) -> list:
    out, seen = [], set()
    for a in soup.select(".mAttach1 a[href], .contents a[href]"):
        href = a["href"]
        if "ND_fileDownload" not in href and not re.search(
                r"\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip)(\?|$)", href, re.I):
            continue
        full = urljoin(page_url, href)
        if full in seen:
            continue
        seen.add(full)
        out.append({"name": a.get_text(strip=True), "url": full})
    return out


def _download(att: dict, referer: str) -> str | None:
    """q_fileId(없으면 파일명) 기준 캐시. 이 사이트는 같은 표시 이름의 첨부가 여럿이라
    파일명만으로 저장하면 서로 덮어쓴다 → '<fileId>__<이름>' 으로 저장."""
    q = parse_qs(urlparse(att["url"]).query)
    fid = (q.get("q_fileId") or [""])[0] or re.sub(r"\W", "_", att["url"].split("/")[-1])
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    hit = next(FILES_DIR.glob(f"{fid}__*"), None)
    if hit:
        return str(hit.relative_to(C.DATA))
    if _denied(att["url"]):
        return None
    r = _get(att["url"], referer, stream=True)
    # 파일명: Content-Disposition → 없으면 URL 의 파일명(확장자 보존 — 추출기가 확장자로 분기함)
    name = (_cd_filename(r.headers.get("Content-Disposition", ""))
            or requests.utils.unquote(urlparse(att["url"]).path.split("/")[-1]) or "file.bin")
    dest = FILES_DIR / (fid + "__" + re.sub(r'[\\/:*?"<>|]', "_", name))
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return str(dest.relative_to(C.DATA))


# ── 정적 페이지 ─────────────────────────────────────────
def _static_targets() -> list:
    """(key, url, kind, camp, title) — 매 실행 다시 받는 상시 정보 페이지 전부."""
    t = []
    for path, name in C.CK_USE_GUIDE:
        t.append((f"use:{path}", C.CK_BASE + path, "use_guide", "", name))
    for no, camp in C.CK_CAMPS.items():
        t.append((f"dept:{camp['dept']}",
                  f"{C.CK_BASE}/user/guide/BD_fcltyGuidance05.do?deptCode={camp['dept']}",
                  "contact", camp["name"], "문의/안내전화"))
    for no, suffix, name in C.CK_CAMP_PAGES:
        path = f"/www/fcltyGuidance/fcltyGuidance{no}/fcltyGuidance{no}{suffix}.jsp"
        t.append((f"camp:{path}", C.CK_BASE + path, "camp_page", C.CK_CAMPS[no]["name"], name))
    trrsrt_camp = {c["trrsrt"]: c["name"] for c in C.CK_CAMPS.values()}
    for trrsrt, upper, fclty, name in C.CK_FACILITY_DETAILS:
        url = (f"{C.CK_BASE}/user/guide/BD_selectFcltyDetailInfo.do?"
               f"trrsrtCode={trrsrt}&upperFcltyCode={upper}" + (f"&fcltyCode={fclty}" if fclty else ""))
        t.append((f"fclty:{trrsrt}:{upper}:{fclty}", url, "facility_detail", trrsrt_camp[trrsrt], name))
    return t


def _crawl_static(store: dict, download: bool, stats: dict, external: dict):
    targets = _static_targets()
    for i, (key, url, kind, camp, title) in enumerate(targets, 1):
        _progress("[예약시스템] 상시 정보 페이지", i, len(targets))
        try:
            html = _fetch(url)
        except Exception as e:
            stats["errors"].append(f"[{camp or '공통'} {title}] 요청 실패 — {e}")
            continue
        soup, root = _content(html)
        if len(root.get_text(strip=True)) < 30:
            stats["errors"].append(f"[{camp or '공통'} {title}] 본문 비어 있음 — {url}")
        html_key = rawhtml.save_html("ck_" + re.sub(r"\W+", "_", key), html)
        atts = _attachments(soup, url)
        if download:
            for att in atts:
                try:
                    att["file"] = _download(att, referer=url)
                except Exception as e:
                    att["file"] = None
                    stats["errors"].append(f"[{title}] 첨부 '{att['name']}' 다운로드 실패 — {e}")
        _record_links(root, url, external)
        store[key] = {"type": kind, "camp": camp, "title": title, "url": url,
                      "html_key": html_key, "attachments": atts}
        stats["static"] += 1
    sys.stdout.write("\n")


# ── 게시판 ──────────────────────────────────────────────
def _list_url(code: str, page: int) -> str:
    return f"{C.CK_BASE}/user/bbs/BD_selectBbsList.do?q_bbsCode={code}&q_currPage={page}"


def _view_url(code: str, sn: str) -> str:
    return f"{C.CK_BASE}/user/bbs/BD_selectBbs.do?q_bbsCode={code}&q_bbscttSn={sn}"


def _scan_list(board: dict, store: dict, full: bool, stats: dict) -> list:
    """목록을 끝까지(또는 새 글이 없는 쪽까지) 넘기며 (sn, 분류, 제목, 날짜) 를 모은다.
    반환: (글 목록, 마지막 쪽까지 도달했는지)"""
    code, name = board["code"], board["name"]
    picked, seen, page, reached_end = [], set(), 1, False
    cap = C.CK_MAX_LIST_PAGES
    while cap is None or page <= cap:
        url = _list_url(code, page)
        try:
            soup, root = _content(_fetch(url))
        except Exception as e:
            stats["errors"].append(f"[{name}] 목록 {page}쪽 요청 실패 — {e}")
            break
        new_on_page, any_on_page = 0, 0
        for a in root.find_all("a", onclick=True):
            m = re.search(r"opView\('(\d+)'\)", a["onclick"])
            if not m or m.group(1) in seen:
                continue
            sn = m.group(1)
            seen.add(sn)
            any_on_page += 1
            tds = [td.get_text(" ", strip=True) for td in a.find_parent("tr").find_all("td")]
            date = next((re.search(r"20\d{2}-\d{2}-\d{2}", x).group(0) for x in tds
                         if re.search(r"20\d{2}-\d{2}-\d{2}", x)), "")
            pinned = bool(tds) and tds[0] == "공지"
            picked.append({"sn": sn, "category": tds[1] if len(tds) > 1 else "",
                           "title": a.get_text(" ", strip=True), "date": date,
                           "pinned": pinned, "list_url": url})
            if f"bbs:{code}:{sn}" not in store:
                new_on_page += 1
        _progress(f"[예약시스템 {name}] 목록 스캔", len(picked), "?")
        if any_on_page == 0:
            reached_end = True                      # 마지막 쪽 다음 = 빈 목록
            break
        if not full and page > 1 and new_on_page == 0:
            break                                   # 증분: 이 쪽부터 과거는 전부 수집 완료
        page += 1
    sys.stdout.write("\n")
    return picked, reached_end


def _crawl_board(board: dict, store: dict, download: bool, full: bool, stats: dict) -> bool:
    """반환: 이번 실행으로 과거 글까지 빠짐없이 수집됐는지 (다음 실행부터 증분 조기 종료 허용 여부)"""
    code, name = board["code"], board["name"]
    errors_before = len(stats["errors"])
    posts, reached_end = _scan_list(board, store, full, stats)
    for i, p in enumerate(posts, 1):
        _progress(f"[예약시스템 {name}] 상세 수집", i, len(posts))
        key = f"bbs:{code}:{p['sn']}"
        cached = store.get(key)
        if cached and not C.FORCE_RECRAWL and rawhtml.has_html(cached.get("html_key", "")):
            cached["pinned"] = p["pinned"]          # 상단 고정 여부는 목록 기준으로 갱신
            stats["skipped"] += 1
            continue
        url = _view_url(code, p["sn"])
        try:
            html = _fetch(url, referer=p["list_url"])
        except Exception as e:
            stats["errors"].append(f"[{name}] '{p['title']}' 상세 요청 실패 — {e}")
            continue
        soup, _ = _content(html)
        if not soup.select_one(".mView1") and not soup.select_one(".contents"):
            stats["errors"].append(f"[{name}] '{p['title']}' 상세 형식 아님(본문 영역 없음) — {url}")
            continue
        # (2004~2007년 옛 글은 본문 HTML 이 깨져 .mView1 틀이 무너짐 → 파서가 .contents 로 대체해 읽음)
        html_key = rawhtml.save_html(f"ck_bbs_{code}_{p['sn']}", html)
        atts = _attachments(soup, url)
        if download:
            for att in atts:
                try:
                    att["file"] = _download(att, referer=url)
                except Exception as e:
                    att["file"] = None
                    stats["errors"].append(f"[{name}] '{p['title']}' 첨부 '{att['name']}' 다운로드 실패 — {e}")
        store[key] = {"type": "bbs", "board": name, "code": code, "sn": p["sn"],
                      "category": p["category"], "title": p["title"], "date": p["date"],
                      "pinned": p["pinned"], "url": url, "html_key": html_key,
                      "attachments": atts}
        stats["new"] += 1
    if posts:
        sys.stdout.write("\n")
    return reached_end and len(stats["errors"]) == errors_before


def _crawl_course_board(board: dict, store: dict, stats: dict, external: dict):
    """여행코스: 목록 카드가 전부. 카드 링크(visitkorea)는 방문하지 않고 기록만."""
    code, name = board["code"], board["name"]
    page, n = 1, 0
    while C.CK_MAX_LIST_PAGES is None or page <= C.CK_MAX_LIST_PAGES:
        url = _list_url(code, page)
        try:
            html = _fetch(url)
        except Exception as e:
            stats["errors"].append(f"[{name}] 목록 {page}쪽 요청 실패 — {e}")
            break
        soup, root = _content(html)
        cards = [li for li in root.select("li") if li.select_one("strong.ti")]
        if not cards:
            break
        html_key = rawhtml.save_html(f"ck_bbs_{code}_list_{page}", html)
        _record_links(root, url, external)
        store[f"bbslist:{code}:{page}"] = {"type": "course_list", "board": name, "code": code,
                                           "page": page, "url": url, "html_key": html_key}
        n += len(cards)
        # 페이지 번호 링크가 없으면 마지막 쪽
        if not root.find("a", onclick=re.compile(rf"opMovePage\({page + 1}\)")):
            break
        page += 1
    stats["courses"] = n


# ── 진입점 ──────────────────────────────────────────────
def run_crawl_camping(download=None, full_reset: bool = False):
    if download is None:
        download = C.DOWNLOAD_ATTACHMENTS
    C.RAW_DIR.mkdir(parents=True, exist_ok=True)
    store = {}
    if MANIFEST.exists() and not full_reset:
        store = {it["_key"]: it for it in json.loads(MANIFEST.read_text(encoding="utf-8"))}

    # 게시판별 '과거 글까지 완료' 상태. 완료 전(첫 실행이 중간에 실패한 경우 포함)에는 목록을 끝까지
    # 다시 훑어 빠진 옛 글을 채우고, 완료된 게시판만 '새 글 없는 쪽에서 멈춤' 증분을 쓴다.
    state = {} if full_reset or not STATE.exists() else json.loads(STATE.read_text(encoding="utf-8"))
    stats = {"static": 0, "new": 0, "skipped": 0, "courses": 0, "errors": []}
    external = {}
    _crawl_static(store, download, stats, external)
    for board in C.CK_BOARDS:
        if board["code"] == "1010":
            _crawl_course_board(board, store, stats, external)
        else:
            done = _crawl_board(board, store, download, not state.get(board["code"]), stats)
            state[board["code"]] = bool(state.get(board["code"])) or (done and C.CK_MAX_LIST_PAGES is None)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = [{**it, "_key": k} for k, it in store.items()]
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    EXTERNAL.write_text(json.dumps([{"url": u, "found_on": f} for u, f in sorted(external.items())],
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[예약시스템 수집 완료] 총 {len(manifest)}건 "
          f"(상시 페이지 {stats['static']} · 게시글 신규 {stats['new']} · 스킵 {stats['skipped']}"
          f" · 여행코스 카드 {stats['courses']})")
    print(f"   ↳ 외부 링크 {len(external)}건 → {EXTERNAL.name} 에 URL 만 기록")
    if stats["errors"]:
        print(f"⚠ 수집 실패/경고 {len(stats['errors'])}건:")
        for e in stats["errors"]:
            print("   -", e)
    else:
        print("✔ 실패 없음")
    return manifest


if __name__ == "__main__":
    run_crawl_camping()
