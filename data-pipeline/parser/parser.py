#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2단계 — 구조화(parser)
====================================================================
역할: data/raw/ 의 원본 HTML 을 읽어 제목/날짜/본문/표/첨부 등
      구조화된 문서(dict)로 변환한다. 네트워크 접근 없음(오프라인).
출력: data/parsed/documents.json  (문서 리스트)

표 처리:
  병합 셀(rowspan/colspan)을 격자(grid)로 펼쳐 값을 채운다.
  → 병합으로 열이 밀리는 문제 해결. 레이아웃용 중첩 표는 '말단 표'만 처리한다.
셀렉터가 사이트 개편으로 안 맞으면 아래 함수만 고치면 된다.
"""

import json
import re

from bs4 import BeautifulSoup

import config as C
import rawhtml


# ── 고정 키 / 안내 링크 ─────────────────────────────────
# 게시글 URL 의 mv_data 에는 목록 위치(startPage·listNo)와 빈 검색 상태가 들어 있어, 새 글이 올라올 때마다
# 같은 글의 URL 이 바뀐다 → 증분 비교·중복 제외 목록·OCR 매칭이 어긋난다.
#   · 문서 고정 키(doc id): 게시글 board-<code>-<idx>, 시설 facility-<idx>, 페이지 page-<정규화 URL md5>
#   · 안내 링크(metadata.url): 목록 상태를 뺀 정규화 URL — Referer 없이 열리는 것 확인(2026-10, 게시판 4종)
_LIST_STATE = {"startPage", "listNo", "search_item", "search_order", "order_list",
               "list_scale", "view_level", "view_cate", "view_cate2"}


def _mv_params(url: str) -> dict:
    m = re.search(r"mv_data=([^&#]+)", url)
    if not m:
        return {}
    from urllib.parse import unquote, parse_qsl
    raw = unquote(m.group(1)).replace("||", "")
    try:
        import base64
        return dict(parse_qsl(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"),
                              keep_blank_values=True))
    except Exception:
        return {}


def canonical_url(url: str) -> str:
    """목록 상태(startPage·listNo 등)가 든 sub.htm URL → 그 상태를 뺀 고정 URL. 해당 없으면 그대로."""
    if "sub.htm" not in url:
        return url
    p = _mv_params(url)
    if not p or not ({"startPage", "listNo"} & p.keys()):
        return url
    import base64
    from urllib.parse import urlencode
    keep = {k: v for k, v in p.items() if k not in _LIST_STATE}
    mv = base64.b64encode(urlencode(keep).encode("utf-8")).decode("ascii")
    head = "mode=view&" if ("mode=view" in url or "idx" in keep) else ""
    return f"{C.SUB}?{head}mv_data={mv}%7C%7C"   # '|' 대신 %7C — 카카오톡 자동 링크가 | 에서 끊김


def page_doc_id(url: str) -> str:
    import hashlib as _h
    return "page-" + _h.md5(canonical_url(url).encode("utf-8")).hexdigest()[:10]


def _content_root(soup):
    return soup.select_one("#sub-content") or soup.body or soup


def _clean(node) -> str:
    if node is None:
        return ""
    for t in node.find_all(["script", "style"]):
        t.decompose()
    txt = node.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


# ── 표: 병합 셀을 격자로 펼치기 ─────────────────────────
def _table_to_grid(table) -> list:
    """rowspan/colspan 을 반영해 직사각형 격자로 변환. 병합 셀 값은 펼쳐서 채움."""
    occupied = {}          # (row, col) -> text
    ncols = 0
    r = 0
    for tr in table.find_all("tr"):
        c = 0
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:      # tbody/thead 등으로 한 겹 더 감싼 경우 대비
            cells = tr.find_all(["td", "th"])
        for cell in cells:
            while (r, c) in occupied:      # 위 행 rowspan 이 차지한 칸 건너뛰기
                c += 1
            text = cell.get_text(" ", strip=True)
            rs = int(cell.get("rowspan", 1) or 1)
            cs = int(cell.get("colspan", 1) or 1)
            for dr in range(rs):
                for dc in range(cs):
                    occupied[(r + dr, c + dc)] = text   # 병합 값을 모든 칸에 펼침
            c += cs
            ncols = max(ncols, c)
        r += 1
    nrows = r
    grid = [[occupied.get((i, j), "") for j in range(ncols)] for i in range(nrows)]
    return [row for row in grid if any(cell.strip() for cell in row)]


def _tables(root) -> list:
    """말단 표(다른 표를 품지 않은 표)만 격자로 변환해 반환."""
    out = []
    for tbl in root.find_all("table"):
        if tbl.find("table"):     # 레이아웃용 컨테이너 표는 건너뛰고 안쪽만
            continue
        grid = _table_to_grid(tbl)
        if grid:
            out.append(grid)
    return out


def _parse_board(soup, item) -> dict:
    root = _content_root(soup)
    tables = _tables(root)                 # 표 먼저 추출
    for t in root.find_all("table"):       # 본문에서 표 제거 → 텍스트 중복 방지
        t.decompose()
    return {
        "id": f"board-{item['code']}-{item['idx']}",
        "source_type": "board",
        "board": item["board"],
        "title": item.get("title", ""),    # 목록 제목이 가장 신뢰도 높음
        "date": item.get("date", ""),
        "url": canonical_url(item["url"]),      # 안내 링크: 목록 위치를 뺀 고정 URL
        "body": _clean(root),
        "tables": tables,
        "attachments": item.get("attachments", []),
    }


def _parse_facility(soup, item) -> dict:
    root = _content_root(soup)
    images = [img["src"] for img in root.find_all("img", src=True) if "/data/" in img["src"]]
    tables = _tables(root)                 # 표 먼저 추출
    for t in root.find_all("table"):       # 본문에서 표 제거 → 텍스트 중복 방지
        t.decompose()
    return {
        "id": f"facility-{item['idx']}",
        "source_type": "facility",
        "facility": item["facility"],
        "ftype": item.get("ftype", ""),
        "title": item.get("facility", ""),   # 실제 시설명(자동 발견 시 메뉴 링크 텍스트)
        "url": item["url"],
        "body": _clean(root),
        "tables": tables,
        "images": images,
        "attachments": [],
    }


def _parse_page(soup, item) -> dict:
    root = _content_root(soup)
    tables = _tables(root)
    for t in root.find_all("table"):
        t.decompose()
    # 제목: <title> 또는 첫 h1/h2
    title = ""
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)
    else:
        h = root.find(["h1", "h2"])
        title = h.get_text(strip=True) if h else item["url"]
    return {
        "id": page_doc_id(item["url"]),          # 고정 키: 목록 위치가 바뀌어도 같은 페이지면 같은 id
        "source_type": "page",
        "kind": item.get("kind", "internal"),
        "title": title,
        "url": canonical_url(item["url"]),
        "body": _clean(root),
        "tables": tables,
        "attachments": item.get("attachments", []),
    }


def parse_html(html: str, item: dict) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    if item["type"] == "board":
        return _parse_board(soup, item)
    if item["type"] == "page":
        return _parse_page(soup, item)
    return _parse_facility(soup, item)


def run_parse():
    manifest_path = C.RAW_DIR / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError("manifest.json 없음 — 먼저 crawler 를 실행하세요.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    docs, seen, merged = [], set(), 0
    for item in manifest:
        html = rawhtml.load_html(item.get("html_key"))
        if not html:
            print(f"  ! 원본 HTML 없음: {item.get('html_key')} (crawler 재실행 필요)")
            continue
        doc = parse_html(html, item)
        # 목록 위치만 다른 같은 글/페이지(정규화 후 같은 고정 키)는 하나만 남긴다
        if doc["id"] in seen:
            merged += 1
            continue
        seen.add(doc["id"])
        docs.append(doc)
    if merged:
        print(f"  ↳ 고정 키가 같은 사본 {merged}건 합침 (목록 위치만 다른 URL)")

    C.PARSED_DIR.mkdir(parents=True, exist_ok=True)
    out = C.PARSED_DIR / "documents.json"
    out.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[parser] 완료 — 문서 {len(docs)}건 → {out.name}")
    return docs


if __name__ == "__main__":
    run_parse()