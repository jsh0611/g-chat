#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1단계 — 수집(crawler)  ※ 증분(incremental) 방식
====================================================================
역할: 사이트에서 HTML(원본)과 첨부파일을 받아 data/raw/ 에 저장한다.
      네트워크 접근은 '이 모듈에서만' 일어난다. (parser/preprocessor 는 오프라인)

출력 방침
  · 무엇을 받는지 일일이 보여주지 않고 '진행 상황(카운터)'만 표시한다.
  · 수집에 실패한 항목만 마지막에 '무엇이 왜 실패했는지' 모아 요약한다.

이 사이트의 게시판 규칙
  · 목록 첫 페이지 : sub.htm?nav_code=<nav>            (mv_data 없음)
  · 목록 2쪽 이후   : sub.htm?mv_data=<startPage=10..>
  · 상세(view)     : sub.htm?mode=view&mv_data=<base64>  (Referer=목록 필요)

재실행 정책(증분): 이미 수집한 글은 재요청하지 않고 병합. 첨부는 파일명 같으면 캐시.
"""

import base64
import json
import re
import sys
import time
from urllib.parse import urlencode, parse_qs, urljoin, urlparse
from collections import deque
import hashlib

import requests
from bs4 import BeautifulSoup

import config as C
import rawhtml

session = requests.Session()
session.headers.update(C.HEADERS)


# ── mv_data 인코딩/디코딩 ───────────────────────────────
def encode_mv(params: dict) -> str:
    qs = urlencode(params)
    return base64.b64encode(qs.encode("utf-8")).decode("ascii") + "||"


def decode_mv(mv: str) -> dict:
    mv = mv.replace("%3D", "=").replace("%7C", "|").replace("||", "").strip()
    pad = "=" * (-len(mv) % 4)
    try:
        raw = base64.b64decode(mv + pad).decode("utf-8", "replace")
    except Exception:
        return {}
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def build_url(nav_code, params: dict) -> str:
    q = {}
    if nav_code:
        q["nav_code"] = nav_code
    q["mv_data"] = encode_mv(params)
    return C.SUB + "?" + urlencode(q)


# ── 진행 표시 ───────────────────────────────────────────
def _progress(prefix: str, cur: int, total) -> None:
    total_s = total if total not in (None, float("inf")) else "?"
    sys.stdout.write(f"\r  {prefix} {cur}/{total_s}   ")
    sys.stdout.flush()


# ── 병합 키 ─────────────────────────────────────────────
def _key(item: dict) -> str:
    if item["type"] == "board":
        return f"board:{item['code']}:{item['idx']}"
    if item["type"] == "page":
        return f"page:{item['url']}"
    return f"facility:{item['idx']}"


# ── HTTP ────────────────────────────────────────────────
def _get_text(url: str, referer: str | None = None) -> str:
    time.sleep(C.DELAY)
    headers = {"Referer": referer} if referer else {}
    r = session.get(url, headers=headers, timeout=C.TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def _cd_filename(cd: str) -> str | None:
    """Content-Disposition에서 파일명을 꺼낸다.
    이 사이트(그리고 옛 관공서 게시판 다수)는 filename 을 퍼센트 인코딩 없이
    EUC-KR 바이트 그대로 헤더에 실어 보낸다. requests/urllib3 는 HTTP 헤더를
    항상 latin-1 로 디코딩하므로, 그 결과를 그대로 쓰면 '¾÷¹«...' 식으로 깨진다.
    → latin-1 로 되돌려 원본 바이트를 복원한 뒤 EUC-KR 로 다시 디코딩한다.
    RFC5987 형식(filename*=UTF-8''%EC...)은 이미 올바른 UTF-8 이므로 그대로 unquote."""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if m:
        return requests.utils.unquote(m.group(1), encoding="utf-8")
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    if not m:
        return None
    name = requests.utils.unquote(m.group(1))
    try:
        name = name.encode("latin-1").decode("euc-kr")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass   # 이미 정상 유니코드였던 경우(latin-1 재인코딩 불가) → 그대로 사용
    return name


def _download(url: str, referer: str | None = None):
    """첨부 다운로드. 성공 시 상대경로, 실패 시 None(호출부에서 실패 기록)."""
    fname = re.sub(r'[\\/:*?"<>|]', "_", url.split("?")[0].split("/")[-1]) or "file.bin"
    dest = C.RAW_FILES / fname
    if dest.exists():
        return str(dest.relative_to(C.DATA))
    r = session.get(url, headers={"Referer": referer} if referer else {},
                    timeout=C.TIMEOUT, stream=True)
    r.raise_for_status()
    cd = r.headers.get("Content-Disposition", "")
    name = _cd_filename(cd)
    if name:
        fname = re.sub(r'[\\/:*?"<>|]', "_", name)
    dest = C.RAW_FILES / fname
    C.RAW_FILES.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return str(dest.relative_to(C.DATA))


def _save_html(key: str, html: str) -> str:
    return rawhtml.save_html(key, html)   # data/raw/raw.db 에 저장, key 반환


def _find_attachments(html: str, page_url: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#sub-content") or soup
    out, seen = [], set()
    for a in root.find_all("a", href=True):
        href = a["href"]
        if re.search(r"(bbs_download|download=|/data/upload/)", href, re.I) or \
           re.search(r"\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip)(\?|$)", href, re.I):
            full = urljoin(page_url, href)
            if full in seen:
                continue
            seen.add(full)
            name = a.get_text(strip=True)
            out.append({"name": "" if name in ("DOWNLOAD", "#", "첨부파일 다운로드") else name,
                        "url": full})
    return out


def _list_url(nav: str, code: str, start_page: int) -> str:
    if start_page == 0:
        return f"{C.SUB}?nav_code={nav}"
    return build_url(None, {"startPage": start_page, "nav_code": nav,
                            "site_code": "newdhsisul", "code": code,
                            "table": "cs_bbs_data"})


# ── 게시판 수집 (code 자동 감지) ────────────────────────
def _crawl_board(board: dict, store: dict, download: bool, stats: dict):
    name, nav = board["name"], board["nav_code"]
    limit = C.MAX_POSTS_PER_BOARD or float("inf")

    # 1) 목록에서 글 링크 수집 (code 는 첫 글의 mv_data 에서 자동 감지)
    picked, seen, code = [], set(), None
    start_page = 0
    while len(picked) < limit:
        if start_page == 0:
            list_url = f"{C.SUB}?nav_code={nav}"
        else:
            list_url = build_url(None, {"startPage": start_page, "nav_code": nav,
                                        "site_code": "newdhsisul", "code": code or "",
                                        "table": "cs_bbs_data"})
        try:
            soup = BeautifulSoup(_get_text(list_url, referer=C.BASE + "/pages/index.htm"),
                                 "html.parser")
        except Exception as e:
            stats["errors"].append(f"[{name}] 목록 {start_page}p 요청 실패 — {e}")
            break
        before = len(picked)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "mode=view" not in href:
                continue
            m = re.search(r"mv_data=([^&\"']+)", href)
            if not m:
                continue
            p = decode_mv(m.group(1))
            idx = p.get("idx")
            if not idx or idx in seen:
                continue
            seen.add(idx)
            c = p.get("code") or "board"
            if code is None:
                code = c
            row = a.find_parent(["tr", "li"])
            rowtxt = row.get_text(" ", strip=True) if row else a.get_text(strip=True)
            dm = re.search(r"(20\d{2})[.\-](\d{1,2})[.\-](\d{1,2})", rowtxt)
            date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
            picked.append({"idx": idx, "code": c, "title": a.get_text(strip=True) or f"idx {idx}",
                           "date": date, "url": urljoin(C.SUB, href), "list_url": list_url})
            if len(picked) >= limit:
                break
        _progress(f"[{name}] 목록 스캔", len(picked), limit)
        if len(picked) == before:
            break
        start_page += 10
    sys.stdout.write("\n")

    if not picked:
        # 게시판이 아니거나(정보 페이지) 글이 없음 → 실패 아님(스파이더가 일반 페이지로 수집)
        return

    # 2) 각 글 상세 수집
    total = len(picked)
    for i, post in enumerate(picked, 1):
        _progress(f"[{name}] 수집", i, total)
        code = post["code"]
        key = f"board:{code}:{post['idx']}"
        cached = store.get(key)
        if cached and not C.FORCE_RECRAWL and rawhtml.has_html(cached.get("html_key", "")):
            stats["skipped"] += 1
            continue
        try:
            html = _get_text(post["url"], referer=post["list_url"])
        except Exception as e:
            stats["errors"].append(f"[{name}] '{post['title']}' 상세 요청 실패 — {e}")
            continue
        if "잘못된 접근" in html:
            stats["errors"].append(f"[{name}] '{post['title']}' 상세 접근 거부(Referer)")
            continue
        html_key = _save_html(f"board_{code}_{post['idx']}", html)
        atts = _find_attachments(html, post["url"])
        if download:
            for att in atts:
                try:
                    att["file"] = _download(att["url"], referer=post["url"])
                except Exception as e:
                    att["file"] = None
                    label = att["name"] or att["url"].split("/")[-1]
                    stats["errors"].append(f"[{name}] '{post['title']}' 첨부 '{label}' 다운로드 실패 — {e}")
        store[key] = {"type": "board", "board": name, "code": code,
                      "idx": post["idx"], "title": post["title"],
                      "date": post["date"], "url": post["url"],
                      "html_key": html_key, "attachments": atts}
        stats["new"] += 1
    sys.stdout.write("\n")


# ── 관리시설안내: 모든 시설 자동 발견·수집 ──────────────
def _discover_facilities(store: dict, download: bool, stats: dict):
    """메인 페이지 메뉴에서 FACILITY_NAVS 아래 '모든 시설'을 자동으로 찾아 수집한다.
       상세는 FACILITY_DETAIL_NAV+idx 로 접근(시설명 h1 이 정확히 나옴)."""
    if not getattr(C, "FACILITY_NAVS", None):
        return
    try:
        soup = BeautifulSoup(_get_text(C.BASE + "/pages/index.htm", referer=C.BASE),
                             "html.parser")
    except Exception as e:
        stats["errors"].append(f"[관리시설안내] 메뉴 로드 실패 — {e}")
        return
    facs = {}   # idx -> (type, name)  (메뉴 submenu 링크 = 정확한 시설명)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "||" not in href and "%7C%7C" not in href:      # 시설 상세 링크만(카테고리 landing 제외)
            continue
        mnav = re.search(r"nav_code=(new\d+)", href)
        mmv = re.search(r"mv_data=([^&\"']+)", href)
        if not mnav or not mmv or mnav.group(1) not in C.FACILITY_NAVS:
            continue
        p = decode_mv(mmv.group(1))
        idx, nm = p.get("idx"), a.get_text(strip=True)
        if not idx or not nm or nm == "자세히보기":
            continue
        facs.setdefault(idx, (p.get("type", ""), nm))

    total = len(facs)
    for i, (idx, (ftype, name)) in enumerate(facs.items(), 1):
        _progress("[관리시설안내] 시설", i, total)
        url = build_url(C.FACILITY_DETAIL_NAV, {"idx": idx})
        try:
            html = _get_text(url, referer=C.SUB + f"?nav_code={C.FACILITY_DETAIL_NAV}")
        except Exception as e:
            stats["errors"].append(f"[관리시설안내] '{name}' 요청 실패 — {e}")
            continue
        html_key = _save_html(f"facility_{idx}", html)
        atts = _find_attachments(html, url)
        if download:
            for att in atts:
                try:
                    att["file"] = _download(att["url"], referer=url)
                except Exception:
                    att["file"] = None
        store[f"facility:{idx}"] = {"type": "facility", "facility": name, "ftype": ftype,
                                    "idx": idx, "url": url, "html_key": html_key,
                                    "attachments": atts}
        stats["updated"] += 1
    if total:
        sys.stdout.write("\n")


# ── 링크 팔로우(스파이더) ───────────────────────────────
_DOC_EXT = re.compile(r"\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip|jpe?g|png|gif)(\?|$)", re.I)
# 다운로드 엔드포인트(확장자 없는 URL). 이것을 '페이지'로 따라가면 이미지/파일 '바이너리'를
# HTML 로 저장해 문서 본문이 깨지고 용량이 폭증한다. → 스파이더가 따라가지 않게 제외.
# (첨부는 _find_attachments 가 같은 패턴으로 인식해 '파일'로 내려받는다.)
_DOWNLOAD_PAT = re.compile(r"(bbs_download|[?&]download=|/data/upload/)", re.I)


def _same_site(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == C.INTERNAL_HOST or host.endswith("." + C.INTERNAL_HOST)


def _norm_url(url: str) -> str:
    return url.split("#")[0].strip()


def _extract_links(html: str, base_url: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = _norm_url(urljoin(base_url, href))
        if full.startswith(("http://", "https://")) and not _DOC_EXT.search(full) \
                and not _DOWNLOAD_PAT.search(full):
            out.append(full)
    return out


def _page_id(url: str) -> str:
    return "page-" + hashlib.md5(url.encode("utf-8")).hexdigest()[:10]


def _excluded(url: str) -> bool:
    return any(pat in url for pat in C.EXCLUDE_URL_PATTERNS)


def _dedup_key(url: str) -> str:
    """같은 글이 여러 경로(startPage/listNo 차이)로 중복 수집되지 않게 정규화한 키.
       게시글·시설 상세는 idx(+code/type) 기준으로 하나만 취급하고, 목록/일반 페이지는 URL 그대로."""
    if "sub.htm" in url and "mv_data=" in url:
        m = re.search(r"mv_data=([^&]+)", url)
        pr = decode_mv(m.group(1)) if m else {}
        idx = pr.get("idx")
        if idx and (pr.get("mode") == "view" or pr.get("code") or pr.get("type")):
            return f"view:{pr.get('code','')}:{pr.get('type','')}:{idx}"
    return _norm_url(url)


def _spider(store: dict, download: bool, stats: dict):
    """내부(dhsisul.org) 링크만 계속 따라가며 전체 사이트를 수집한다.
       · SEED_URLS(메인 페이지)에서 시작해 모든 내부 링크로 뻗어나감
       · EXCLUDE_URL_PATTERNS 에 걸리는 URL, 비밀번호 입력칸이 있는 로그인 페이지는 건너뜀
       · 같은 글의 중복(startPage/listNo 차이)은 idx 기준으로 1건만 수집
       · 외부 사이트 링크는 방문하지 않고 URL 만 external_links.json 에 기록"""
    if not C.FOLLOW_LINKS:
        return
    # 이미 수집한 것 중 '내용 페이지(게시글·시설)'만 방문완료로 등록해 재요청을 건너뛴다.
    # 목록/메뉴/페이저 페이지는 등록하지 않아 매 실행 다시 훑고 → 새 글을 발견한다(증분).
    visited = set()
    for it in store.values():
        u = _norm_url(it.get("url", ""))
        if not u:
            continue
        k = _dedup_key(u)
        if k.startswith("view:"):     # 내용 페이지만
            visited.add(k)
    external = {}          # 외부 url -> 발견 위치
    frontier = deque()     # (url, referer)

    def _consider(links, found_on):
        for link in links:
            if _excluded(link):
                continue
            if _same_site(link):
                k = _dedup_key(link)
                if k in visited:
                    continue
                visited.add(k)
                frontier.append((link, found_on))
            else:
                external.setdefault(link, found_on)

    # 시드: 메인 페이지들
    for su in C.SEED_URLS:
        if _same_site(su) and not _excluded(su):
            k = _dedup_key(su)
            if k not in visited:
                visited.add(k)
                frontier.append((su, C.BASE))
    # 시드: 이미 수집한 페이지들의 링크
    for it in list(store.values()):
        html = rawhtml.load_html(it.get("html_key")) if it.get("html_key") else None
        if html:
            _consider(_extract_links(html, it.get("url", C.BASE)), it.get("url", C.BASE))

    cap = C.MAX_SPIDER_PAGES
    pages = 0
    while frontier and (cap is None or pages < cap):
        url, referer = frontier.popleft()
        try:
            html = _get_text(url, referer=referer)
        except Exception as e:
            stats["errors"].append(f"[링크] {url} 요청 실패 — {e}")
            continue
        if "잘못된 접근" in html:                      # referer 재시도
            try:
                html = _get_text(url, referer=url)
            except Exception:
                pass
        # 로그인/비밀번호 페이지는 건너뜀
        if re.search(r'type\s*=\s*["\']?password', html, re.I) or "비밀번호를 입력" in html:
            stats["excluded"] = stats.get("excluded", 0) + 1
            continue
        html_key = _save_html(_page_id(url), html)
        atts = _find_attachments(html, url)
        if download:
            for att in atts:
                try:
                    att["file"] = _download(att["url"], referer=url)
                except Exception as e:
                    att["file"] = None
                    stats["errors"].append(f"[링크] {url} 첨부 다운로드 실패 — {e}")
        store[f"page:{url}"] = {"type": "page", "kind": "internal", "url": url,
                                "html_key": html_key, "attachments": atts}
        pages += 1
        stats["pages"] = stats.get("pages", 0) + 1
        _progress("[전체수집] 내부 페이지", pages, cap)
        _consider(_extract_links(html, url), url)
    if pages:
        sys.stdout.write("\n")

    stats["external"] = len(external)
    if external:
        (C.RAW_DIR / "external_links.json").write_text(
            json.dumps([{"url": u, "found_on": f} for u, f in sorted(external.items())],
                       ensure_ascii=False, indent=2), encoding="utf-8")


# ── 진입점 ──────────────────────────────────────────────
def run_crawl(download=None, full_reset: bool = False):
    if download is None:
        download = C.DOWNLOAD_ATTACHMENTS
    C.RAW_DIR.mkdir(parents=True, exist_ok=True)

    mpath = C.RAW_DIR / "manifest.json"
    store = {}
    if mpath.exists() and not full_reset:
        for it in json.loads(mpath.read_text(encoding="utf-8")):
            store[_key(it)] = it

    stats = {"new": 0, "skipped": 0, "updated": 0, "pages": 0, "external": 0, "excluded": 0, "errors": []}
    for board in C.BOARDS:
        _crawl_board(board, store, download, stats)
    _discover_facilities(store, download, stats)
    _spider(store, download, stats)   # 링크 따라가며 추가 수집

    manifest = list(store.values())
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 요약 ──
    print(f"\n[수집 완료] 총 {len(manifest)}건 저장 "
          f"(신규 {stats['new']} · 스킵 {stats['skipped']} · 시설 {stats['updated']} · 내부링크 {stats['pages']})")
    if stats.get("excluded"):
        print(f"   ↳ 로그인/암호 페이지 {stats['excluded']}건 건너뜀")
    if stats.get("external"):
        print(f"   ↳ 외부 사이트 링크 {stats['external']}건 발견 → data/raw/external_links.json 에 기록(전용 크롤러 필요)")
    if stats["errors"]:
        print(f"⚠ 수집 실패 {len(stats['errors'])}건:")
        for e in stats["errors"]:
            print("   -", e)
    else:
        print("✔ 실패 없음")
    return manifest


if __name__ == "__main__":
    run_crawl()