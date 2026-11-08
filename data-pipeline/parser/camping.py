#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2단계 — 구조화: 동해시 관광지 통합예약시스템(campingkorea.or.kr)
====================================================================
입력: data/raw/camping_manifest.json + raw.db('ck_' 키)
출력: data/parsed/documents_camping.json

dhsisul 파서(parser.py)와 다른 점
  · 표를 본문 끝으로 떼어내지 않고 '제자리'에 남긴다. 이 사이트는 "성수기 환불규정" 같은
    소제목 바로 밑에 표가 오고 두 표의 헤더가 똑같아(구분 | 10일전 취소 | …), 떼어내면
    어느 표가 성수기/비수기인지 구분할 수 없게 된다.
  · 빈 셀도 자리를 지킨다(" | " 로 이어 붙임) → ingest 의 표 레코드 변환에서 열이 밀리지 않음.
  · 인라인 태그(<strong> 등)에서 줄을 끊지 않는다. "예약은 / 오늘날짜 … / 하실 수 있으며"
    처럼 문장이 쪼개지면 청킹·검색 품질이 떨어짐 → 블록 태그와 <br> 에서만 줄바꿈.
"""

import json
import re

from bs4 import BeautifulSoup, NavigableString

import config as C
import rawhtml

MANIFEST = C.RAW_DIR / "camping_manifest.json"
OUT = C.PARSED_DIR / "documents_camping.json"

_BLOCK = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
          "tr", "table", "dl", "dt", "dd", "section", "article"}
# 본문에서 걷어낼 UI 요소: 경로(HOME > …), 탭, 숨은 폼 코드, VR·지도·다운로드 버튼
_DROP = [".mBc1", ".mTab3", ".invisible", ".mButton1", ".mImg2", "script", "style",
         "form", "a[href^='javascript:vrPop']", "caption"]


def _span(v) -> int:
    """rowspan/colspan 값. 게시판 편집기가 rowspan='\\"2\\"' 처럼 따옴표를 겹쳐 넣은 글이 있어 숫자만 읽는다."""
    m = re.search(r"\d+", str(v or ""))
    return max(1, min(int(m.group(0)), 200)) if m else 1


def _grid_lines(table) -> str:
    """병합 셀 표 → ' | ' 행. (parser._table_to_grid 와 같은 펼치기에 두 가지를 더함)
      · 다단 헤더(thead 여러 줄 / th 만 있는 선두 행)는 열마다 한 줄로 합친다.
          예) 사용료 / 비수기 / 평일 → "사용료 비수기 평일"
        table_utils 는 헤더를 최대 2단까지만 합치므로, 3단 헤더가 오면 셋째 줄을 데이터로 오인한다.
      · colspan 으로 가로 복제된 값은, 같은 헤더 라벨 아래 복제면 비운다("캐빈 A형 | 캐빈 A형").
        헤더가 다른 열로 번진 복제("1일 100,000원"이 성수기·비수기 모두)는 실제 값이므로 남긴다.
        세로(rowspan) 복제는 행마다 필요한 라벨이라 그대로 둔다."""
    cells_at, is_hdr, r = {}, [], 0
    for tr in table.find_all("tr"):
        c = 0
        tds = tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"])
        for cell in tds:
            while (r, c) in cells_at:
                c += 1
            text = cell.get_text(" ", strip=True).replace("|", "/")
            rs, cs = _span(cell.get("rowspan")), _span(cell.get("colspan"))
            for dr in range(rs):
                for dc in range(cs):
                    cells_at[(r + dr, c + dc)] = (text, dc > 0, c)   # (값, 가로복제?, 원래 열)
            c += cs
        is_hdr.append(tr.find_parent("thead") is not None
                      or (bool(tds) and all(t.name == "th" for t in tds)))
        r += 1
    if not cells_at:
        return ""
    ncols = max(c for _, c in cells_at) + 1
    nhdr = 0
    while nhdr < r and is_hdr[nhdr]:
        nhdr += 1
    if nhdr == r:          # 전부 th 인 표 → 첫 줄만 헤더
        nhdr = 1
    header = []
    for j in range(ncols):
        parts = []
        for i in range(nhdr):
            v = cells_at.get((i, j), ("", False, j))[0]
            if v and v not in parts:
                parts.append(v)
        header.append(" ".join(parts))
    out = [" | ".join(header)] if nhdr and ncols > 1 else []
    for i in range(nhdr, r):
        row = []
        for j in range(ncols):
            v, hcopy, origin = cells_at.get((i, j), ("", False, j))
            if hcopy and header and header[j] == header[origin]:
                v = ""
            row.append(v)
        if any(row):
            out.append(row[0] if ncols == 1 else " | ".join(row))
    return "\n".join(out)


def block_text(node) -> str:
    """블록 경계·<br> 에서만 줄바꿈하고, 표는 ' | ' 행으로 제자리에 펼친다."""
    for sel in _DROP:
        for t in node.select(sel):
            t.decompose()
    for tbl in node.find_all("table"):
        if tbl.find("table"):
            continue                                   # 레이아웃 표는 안쪽 표만
        tbl.replace_with(NavigableString("\n\n" + _grid_lines(tbl) + "\n\n"))
    for br in node.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for tag in node.find_all(_BLOCK):
        tag.insert_before(NavigableString("\n"))
        tag.insert_after(NavigableString("\n"))
    text = node.get_text("")
    lines = []
    for ln in text.split("\n"):
        ln = re.sub(r"[ \t 　]+", " ", ln).strip()
        if ln:
            lines.append(ln)
        elif lines and lines[-1] != "":
            lines.append("")                           # 문단 경계 1줄 유지 (표 블록 분리용)
    return "\n".join(lines).strip()


def _page_title(soup) -> str:
    h = soup.select_one(".contents .mTitle1")
    return h.get_text(" ", strip=True) if h else ""


# ── 유형별 파서 ─────────────────────────────────────────
def _tab_panels(root) -> list:
    """탭 페이지(예: 이용요금)면 [(탭 이름, 패널 노드)], 아니면 [].
    탭 버튼(.mTab1 li) 과 패널(.useGuidanceList) 이 순서대로 1:1 대응한다."""
    tabs = [a.get_text(" ", strip=True) for a in root.select(".mTab1 li a")]
    panels = root.select(".useGuidanceList")
    if tabs and len(tabs) == len(panels):
        return list(zip(tabs, panels))
    return []


def _parse_static(item: dict, html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".contents") or soup.body
    real_title = _page_title(soup)
    for t in root.select(".gTitle1"):
        t.decompose()
    kind, camp, name = item["type"], item.get("camp", ""), item["title"]
    doc_id = "ck-" + re.sub(r"[^0-9A-Za-z]+", "-", item["_key"].split(":", 1)[1]).strip("-")
    base = {"source_type": "facility", "kind": kind, "url": item["url"], "tables": [],
            "attachments": item.get("attachments", []), "reserve_url": C.CK_RESERVE_URL}

    # 탭 페이지 → 캠핑장별 문서로 분리 ("추암 요금" 질문이 추암 탭 문서의 제목에 바로 걸리도록)
    panels = _tab_panels(root)
    if panels:
        docs = []
        for i, (tab, panel) in enumerate(panels, 1):
            docs.append({**base, "id": f"{doc_id}-{i}", "category": tab, "camp": tab,
                         "title": f"{tab} {real_title or name}",
                         "body": block_text(panel),
                         "attachments": base["attachments"] if i == 1 else []})
        return docs

    body = block_text(root)
    if kind == "use_guide":
        title, category = f"캠핑장 {real_title or name}", "캠핑장 공통 이용안내"
        body = ("※ 망상오토캠핑리조트·망상제2오토캠핑장·무릉힐링캠프장·추암오토캠핑장 공통 "
                "(동해시 관광지 통합예약시스템)\n" + body)
    elif kind == "contact":
        title, category = f"{camp} 문의/안내전화", camp
    else:   # camp_page · facility_detail
        title, category = f"{camp} {real_title or name}", camp
    return [{**base, "id": doc_id, "category": category, "camp": camp,
             "title": title, "body": body}]


def _parse_bbs(item: dict, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    view = soup.select_one(".mView1")
    if view is None:
        # 옛 글(2004~2007): 게시물 HTML 이 깨져 .mView1 틀이 무너짐 → 목록의 제목·날짜 + .contents 본문
        root = soup.select_one(".contents")
        for t in root.select(".gTitle1"):
            t.decompose()
        cat = item.get("category", "")
        return {"id": f"ck-bbs-{item['code']}-{item['sn']}", "source_type": "board",
                "kind": "notice" if item["code"] == "1001" else "faq", "board": item["board"],
                "category": cat, "title": f"[{cat}] {item['title']}" if cat else item["title"],
                "date": item.get("date", ""), "url": item["url"], "body": block_text(root),
                "tables": [], "images": [], "pinned": item.get("pinned", False),
                "attachments": item.get("attachments", [])}
    tit = view.select_one(".tit")
    cat_el = tit.select_one(".text-info")
    category = re.sub(r"[\[\]\s]+", " ", cat_el.get_text(" ", strip=True)).strip() if cat_el else ""
    if cat_el:
        cat_el.decompose()
    title = tit.get_text(" ", strip=True) or item["title"]
    date = item.get("date", "")
    for col in view.select(".util .col"):
        ti, tx = col.select_one(".ti"), col.select_one(".tx")
        if ti and tx and "등록일" in ti.get_text():
            m = re.search(r"20\d{2}-\d{2}-\d{2}", tx.get_text())
            date = m.group(0) if m else date
    art = view.select_one(".article")
    body = block_text(art) if art else ""
    images = [img.get("src") for img in (art.find_all("img") if art else []) if img.get("src")]
    category = category or item.get("category", "")
    return {"id": f"ck-bbs-{item['code']}-{item['sn']}", "source_type": "board",
            "kind": "notice" if item["code"] == "1001" else "faq",
            "board": item["board"], "category": category,
            "title": f"[{category}] {title}" if category else title,
            "date": date, "url": item["url"], "body": body, "tables": [],
            "images": images, "pinned": item.get("pinned", False),
            "attachments": item.get("attachments", [])}


def _parse_courses(item: dict, html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(".contents")
    docs = []
    for li in root.select("li"):
        ti = li.select_one("strong.ti")
        if not ti:
            continue
        a = li.find("a", href=True)
        dist = li.select_one(".img .tx")
        desc = li.select(".txt .tx")
        tags = [t.get_text(strip=True) for t in li.select(".tag span, .tag a, .hash span")]
        text_desc = " ".join(d.get_text(" ", strip=True) for d in desc)
        if not tags:                                   # 태그 마크업이 다를 때: '#' 로 시작하는 줄
            tags = re.findall(r"#\S+", li.get_text(" ", strip=True))
            text_desc = re.sub(r"#\S+", "", text_desc).strip()
        cotid = re.search(r"cotid=([\w-]+)", a["href"]) if a else None
        body = "\n".join(x for x in [
            (dist.get_text(" ", strip=True) if dist else ""),
            text_desc,
            " ".join(tags),
            f"코스 상세(한국관광공사 대한민국 구석구석): {a['href']}" if a else "",
        ] if x)
        docs.append({"id": f"ck-course-{cotid.group(1)[:8] if cotid else len(docs)}",
                     "source_type": "page", "kind": "course", "board": item["board"],
                     "category": "여행코스", "title": f"동해 여행코스: {ti.get_text(strip=True)}",
                     "url": item["url"], "external_url": a["href"] if a else "",
                     "body": body, "tables": [], "attachments": []})
    return docs


def run_parse_camping():
    if not MANIFEST.exists():
        raise FileNotFoundError("camping_manifest.json 없음 — 먼저 'python main.py camping-crawl' 실행")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    docs, missing = [], 0
    for item in manifest:
        html = rawhtml.load_html(item.get("html_key"))
        if not html:
            missing += 1
            continue
        if item["type"] == "bbs":
            docs.append(_parse_bbs(item, html))
        elif item["type"] == "course_list":
            docs.extend(_parse_courses(item, html))
        else:
            docs.extend(_parse_static(item, html))
    C.PARSED_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[parser:예약시스템] 완료 — 문서 {len(docs)}건 → {OUT.name}"
          + (f" (원본 없음 {missing}건)" if missing else ""))
    return docs


if __name__ == "__main__":
    run_parse_camping()
