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
        "url": item["url"],
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
    import hashlib as _h
    return {
        "id": "page-" + _h.md5(item["url"].encode("utf-8")).hexdigest()[:10],
        "source_type": "page",
        "kind": item.get("kind", "internal"),
        "title": title,
        "url": item["url"],
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

    docs = []
    for item in manifest:
        html = rawhtml.load_html(item.get("html_key"))
        if not html:
            print(f"  ! 원본 HTML 없음: {item.get('html_key')} (crawler 재실행 필요)")
            continue
        docs.append(parse_html(html, item))

    C.PARSED_DIR.mkdir(parents=True, exist_ok=True)
    out = C.PARSED_DIR / "documents.json"
    out.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[parser] 완료 — 문서 {len(docs)}건 → {out.name}")
    return docs


if __name__ == "__main__":
    run_parse()