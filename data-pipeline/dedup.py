#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
중복 제거 — 공단 홈페이지(documents.jsonl) + 관광지 통합예약시스템(documents_camping.jsonl)
====================================================================
  python dedup.py            # dry-run: 판정 결과만 보고 (아무것도 바꾸지 않음)
  python dedup.py --apply    # dry-run 과 같은 판정으로 제외 목록 기록 (확인 후에만 실행)

원본 jsonl 은 절대 수정하지 않는다. --apply 는 저장소 루트의 dedup_exclusions.json 에
'적재 때 건너뛸 doc_id' 만 기록하고(ingest.js / ingest_camping.js 가 읽음), 같은 내용을
logs/dedup_applied_<시각>.json 에 남긴다. 되돌리기 = dedup_exclusions.json 에서 항목 삭제 후 재적재.

비교 대상: 적재 단계와 같은 필터를 통과하는 문서만 (대장류 10만 자 초과·옛 공지 컷 제외 — 어차피 DB 에 없음)

판정
  · 정규화: 출처별로 문서의 20% 이상에 반복되는 줄(메뉴·푸터·경로)을 지우고, 공백·줄바꿈 제거
  · 완전 중복: 정규화 본문 해시가 같음
  · 거의 같음: 제목 유사도 ≥ TITLE_SIM 이고 문자 5-gram Jaccard ≥ JACCARD
  · 충돌: 위 둘 중 하나인데 숫자(요금·시간·날짜 등) 집합이 다름 → 둘 다 남기고 차이 표로 보고
  · 부분 중복: 작은 쪽의 5-gram 이 큰 쪽에 CONTAIN 이상 포함(요약 관계) → 둘 다 남기고 목록만 보고
남길 문서
  · 같은 사이트: 원래 메뉴 경로 우선 — 공단은 facility(시설 메뉴) > board > page,
    page 끼리는 mv_data 파라미터가 적은(=메뉴 링크 그대로인) URL 우선
  · 사이트 간: 캠핑장 운영 정보 → campingkorea / 공단 전체 공지(입찰·채용·경영 등) → dhsisul /
    양쪽 공지가 같으면 게시일 최신 → 첨부 있는 쪽
"""

import base64
import hashlib
import json
import re
import sys
import zlib
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

REPO = Path(__file__).resolve().parent.parent
FILES = {"dhsisul": REPO / "documents.jsonl", "campingkorea": REPO / "documents_camping.jsonl"}
OUT_DIR = Path(__file__).resolve().parent / "data" / "dedup"
EXCLUSIONS = REPO / "dedup_exclusions.json"

JACCARD = 0.90        # 거의 같음 기준 (데이터 보고 조정 — 보고서의 분포 참고)
TITLE_SIM = 0.80
CONTAIN = 0.80        # 부분 중복: 작은 문서의 이 비율 이상이 큰 문서에 들어 있음 (같은 사이트)
CONTAIN_CROSS = 0.50  # 사이트 간은 서식이 달라 겹침이 낮게 나옴 — 실측: 공단 망상 시설 페이지 ⊃ 예약시스템 망상 이용요금 0.79,
                      #   컨벤션센터 ⊂ 대관시설 0.66, 무릉 시설 ⊃ 무릉 이용요금 0.61 (그 아래는 0.43 이하로 뚝 떨어짐)
SHINGLE = 5
# 적재 규칙과 동일 (ingest_utils.js 의 LEDGER_TITLE · capAttachments) — 바꾸면 함께 바꿀 것
LEDGER_TITLE = re.compile(r"정보목록|기록물\s*등록\s*대장|비공개\s*대상\s*정보\s*세부기준|"
                          r"정보공개\s*(공개\s*및\s*비공개\s*목록|청구\s*및\s*처리\s*현황)")
ATTACH_CAP = 30_000
_ATT_MARK = re.compile(r"\n\n\[(첨부|이미지): ([^\]\n]*)\]")


# 현행 버전만 넣는 문서 (ingest_utils.js VERSIONED_DOCS 와 동일)
VERSIONED_DOCS = [
    (re.compile(r"^개인정보처리방침$"),
     [re.compile(r"(\d{4})년\s*개인정보\s*처리방침"), re.compile(r"(\d{4})년\s*방침개정")]),
]


def keep_latest_versions(title: str, content: str) -> str:
    rule = next((keep for t, keep in VERSIONED_DOCS if t.search(title or "")), None)
    marks = list(_ATT_MARK.finditer(content))
    if not rule or not marks:
        return content
    def year(re_, name):
        m = re_.search(name)
        return int(m.group(1)) if m else None
    latest = [max([y for y in (year(r, m.group(2)) for m in marks) if y] or [0]) for r in rule]
    out = content[:marks[0].start()]
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        keep = m.group(1) != "첨부" or any(year(r, m.group(2)) == latest[j] for j, r in enumerate(rule))
        if keep:
            out += content[m.start():end]
    return out


def cap_attachments(content: str) -> str:
    marks = list(_ATT_MARK.finditer(content))
    if not any(m.group(1) == "첨부" for m in marks):
        return content
    out = content[:marks[0].start()]
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        block = content[m.start():end]
        head = m.group(0)
        body = block[len(head):]
        if m.group(1) == "첨부" and len(body) > ATTACH_CAP:
            block = f"{head}{body[:ATTACH_CAP]}\n[...첨부 앞부분 {ATTACH_CAP:,}자만 수록 — 전체 {len(body):,}자...]"
        out += block
    return out


BOARD_DATE_CUTOFF = "2025-01-01"
BOILER_RATIO = 0.20
CAMP_RE = re.compile(r"캠핑|캠프|야영|오토캠|캐라반|카라반|글램핑|한옥마을|망상리조트|힐링캠")
ORG_NOTICE_RE = re.compile(r"입찰|채용|합격|공고|경영|인사|계약|감사|예산|규정|윤리|인권|정보공개|정보목록")
NUM_RE = re.compile(r"\d{1,2}:\d{2}|\d{4}[.\-]\d{1,2}[.\-]\d{1,2}|\d[\d,]*(?:\.\d+)?")


# ── 로드 ────────────────────────────────────────────────
def load() -> list:
    """적재 대상 문서. 이미 제외 목록(dedup_exclusions.json)에 있는 문서는 빼서, 매주 dry-run 이
    새로 생긴 중복 후보만 보고하게 한다."""
    docs = []
    already = set()
    if EXCLUSIONS.exists():
        already = {(e["source"], e["doc_id"])
                   for e in json.loads(EXCLUSIONS.read_text(encoding="utf-8"))["excluded"]}
    for src, path in FILES.items():
        if not path.exists():
            print(f"  ! {path.name} 없음 — {src} 제외")
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            o = json.loads(line)
            m = o.get("metadata", {})
            content = o.get("content", "")
            date = (m.get("date") or "")[:10] or None
            # 적재 필터와 동일 (ingest.js / ingest_camping.js)
            if (src, m.get("doc_id") or o["id"]) in already:
                continue
            if LEDGER_TITLE.search(m.get("title") or ""):
                continue
            content = cap_attachments(keep_latest_versions(m.get("title") or "", content))
            if src == "dhsisul" and m.get("source_type") == "board" and m.get("source") in {"공지사항", "채용공고", "행사안내"} and date and date < BOARD_DATE_CUTOFF:
                continue
            if src == "campingkorea" and m.get("kind") == "notice" and not m.get("pinned") \
                    and date and date < BOARD_DATE_CUTOFF:
                continue
            docs.append({"src": src, "id": m.get("doc_id") or o["id"], "type": m.get("source_type"),
                         "kind": m.get("kind"), "title": m.get("title") or "", "url": m.get("url") or "",
                         "date": date, "content": content,
                         "has_att": "[첨부:" in content or bool(m.get("attachments"))})
    return docs


# ── 정규화 ──────────────────────────────────────────────
def boilerplate_lines(docs) -> dict:
    """출처별로 문서의 BOILER_RATIO 이상에 나오는 줄 = 메뉴/푸터/경로 같은 공통 요소."""
    out = {}
    for src in FILES:
        sub = [d for d in docs if d["src"] == src]
        cnt = Counter()
        for d in sub:
            cnt.update({ln.strip() for ln in d["content"].split("\n") if ln.strip()})
        out[src] = {ln for ln, n in cnt.items() if len(sub) >= 20 and n / len(sub) >= BOILER_RATIO}
    return out


def normalize(d, boiler) -> str:
    lines = []
    for ln in d["content"].split("\n"):
        s = ln.strip()
        if not s or s in boiler[d["src"]] or s == "[표]" or s == d["title"].strip():
            continue
        if s.startswith("※ 망상오토캠핑리조트·망상제2오토캠핑장"):   # 예약시스템 공통 머리말(전처리가 붙인 것)
            continue
        lines.append(s)
    return re.sub(r"\s+", "", "".join(lines)).lower()


def norm_title(t: str) -> str:
    t = re.sub(r"^\[[^\]]*\]\s*", "", t)
    return re.sub(r"\s+", "", t).lower()


def shingles(s: str) -> set:
    return {zlib.crc32(s[i:i + SHINGLE].encode("utf-8")) for i in range(max(1, len(s) - SHINGLE + 1))}


def numbers(s: str) -> Counter:
    return Counter(n.replace(",", "") for n in NUM_RE.findall(s))


# ── 후보 쌍: MinHash LSH + 제목 같은 것 + (사이트 간) 캠핑 관련 전수 ────
def minhash_candidates(docs, sh) -> set:
    rng = np.random.default_rng(7)
    P = np.uint64((1 << 61) - 1)
    a = rng.integers(1, 1 << 31, 64, dtype=np.uint64)
    b = rng.integers(0, 1 << 31, 64, dtype=np.uint64)
    bands = defaultdict(list)
    for i, s in enumerate(sh):
        x = np.fromiter(s, dtype=np.uint64)
        sig = ((np.outer(a, x) + b[:, None]) % P).min(axis=1)
        for bi in range(16):
            bands[(bi, sig[bi * 4:(bi + 1) * 4].tobytes())].append(i)
    pairs = set()
    for ids in bands.values():
        if 1 < len(ids) < 200:
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    pairs.add((ids[x], ids[y]))
    return pairs


def candidates(docs, sh) -> set:
    pairs = minhash_candidates(docs, sh)
    by_title = defaultdict(list)
    for i, d in enumerate(docs):
        by_title[norm_title(d["title"])].append(i)
    for ids in by_title.values():
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                pairs.add((ids[x], ids[y]))
    # 사이트 간 부분 중복은 Jaccard 가 낮아 LSH 에 안 걸림 → 캠핑 관련 공단 문서 × 예약시스템 문서 전수 비교
    ck = [i for i, d in enumerate(docs) if d["src"] == "campingkorea"]
    dh = [i for i, d in enumerate(docs) if d["src"] == "dhsisul" and CAMP_RE.search(d["title"] + d["content"][:3000])]
    for i in dh:
        for j in ck:
            pairs.add((min(i, j), max(i, j)))
    return pairs


# ── 남길 문서 선택 ──────────────────────────────────────
def _mv_param_count(url: str) -> int:
    m = re.search(r"mv_data=([^&]+)", unquote(url))
    if not m:
        return 0
    raw = m.group(1).replace("||", "")
    try:
        return len(parse_qs(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")))
    except Exception:
        return 9


def same_site_rank(d) -> tuple:
    type_rank = {"facility": 0, "board": 1, "page": 2}.get(d["type"], 3)
    return (type_rank, _mv_param_count(d["url"]), "%7C" in d["url"],
            -(int(d["date"].replace("-", "")) if d["date"] else 0), -len(d["content"]), len(d["url"]))


def cross_keep(a, b) -> tuple:
    """사이트 간 중복에서 (남길 문서, 이유)."""
    dh, ck = (a, b) if a["src"] == "dhsisul" else (b, a)
    if dh["type"] == "board" and ck["type"] == "board":
        if (dh["date"] or "") != (ck["date"] or ""):
            k = dh if (dh["date"] or "") > (ck["date"] or "") else ck
            return k, "양쪽 같은 공지 → 게시일 최신"
        if dh["has_att"] != ck["has_att"]:
            return (dh if dh["has_att"] else ck), "양쪽 같은 공지 → 첨부 있는 쪽"
    if dh["type"] == "board" and ORG_NOTICE_RE.search(dh["title"]) and not CAMP_RE.search(dh["title"]):
        return dh, "공단 전체 공지 → dhsisul"
    return ck, "캠핑장 운영 정보 → campingkorea"


# ── 판정 ────────────────────────────────────────────────
def num_diff_rows(a, b, limit=6) -> list:
    na, nb = numbers(a["content"]), numbers(b["content"])
    rows = []
    for doc, only in ((a, na - nb), (b, nb - na)):
        flat = doc["content"].replace(",", "")          # numbers() 가 쉼표를 뗀 값이므로 같은 기준으로 찾음
        for n in list(only)[:limit]:
            m = re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", flat)
            ctx = re.sub(r"\s+", " ", flat[max(0, m.start() - 30):m.end() + 20]) if m else ""
            rows.append({"doc": f"{doc['src']}:{doc['id']}", "only_here": n, "context": ctx})
    return rows


def body_text(d) -> str:
    """제목 줄을 뺀 본문 — 제목의 숫자('망상제2오토캠핑장')가 본문 숫자 비교에 섞이지 않게."""
    lines = d["content"].split("\n")
    if lines and lines[0].strip() == d["title"].strip():
        lines = lines[1:]
    return "\n".join(lines)


def _line_key(line: str) -> str:
    """숫자를 뺀 줄 문구 = '같은 항목'을 가리키는 키 (예: '입실시간:#' / '자동차캠프장#성수기')."""
    k = re.sub(r"\d[\d,.:~]*", "#", line)
    return re.sub(r"[\s|/·\-]+", "", k)


def line_conflicts(a, b, limit=12) -> list:
    """같은 항목(숫자 뺀 문구가 같은 줄)인데 숫자가 다른 줄 쌍 — 어떤 항목이 어떻게 다른지."""
    def index(doc):
        out = {}
        for ln in doc["content"].split("\n"):
            ln = ln.strip()
            if not re.search(r"\d", ln):
                continue
            k = _line_key(ln)
            if len(re.sub(r"#", "", k)) >= 4:
                out.setdefault(k, ln)
        return out
    ia, ib = index(a), index(b)
    rows = []
    for k in ia.keys() & ib.keys():
        if sorted(numbers(ia[k]).elements()) != sorted(numbers(ib[k]).elements()):
            rows.append({"item": re.sub(r"#", "○", k)[:60],
                         "a": ia[k][:160], "b": ib[k][:160],
                         "a_doc": f"{a['src']}:{a['id']}", "b_doc": f"{b['src']}:{b['id']}"})

    # 표 행: 두 사이트의 열 구성이 달라(공단 "숙영 시설 | 망상 오토캠핑 리조트 | 캐빈 A형 | 캐빈 A형 | …",
    # 예약시스템 "망상 오토캠핑 리조트 | 캐빈 A형 | | …") 줄 문구로는 짝이 안 맞는다.
    # → 숫자 없는 셀 집합을 항목 이름으로 보고, 한쪽이 다른 쪽에 포함되면 같은 항목.
    #   같은 이름의 행이 여럿이면(F타입 2층형 6인/8인) 숫자가 같은 행이 하나라도 있으면 충돌 아님.
    # 값 셀 = 금액·비율·개수("200,000", "20%", "32,000원") 또는 정원/면적("2인/31.92㎡").
    # "A타입(1층형)", "1일", "대회의실 (350인)" 처럼 숫자가 섞인 이름은 항목 이름(라벨)으로 둔다.
    VALUE = re.compile(r"[\d,.\s]+(원|%|명|대|면)?|\d+인\s*/\s*[\d.,]+㎡")

    def table_rows(doc):
        out = []
        for ln in doc["content"].split("\n"):
            if "|" not in ln or not re.search(r"\d", ln):
                continue
            cells = [c.strip() for c in ln.split("|") if c.strip()]
            vals = [c for c in cells if VALUE.fullmatch(c)]
            label = frozenset(re.sub(r"\s+", "", c) for c in cells if not VALUE.fullmatch(c))
            nums = frozenset(numbers(" ".join(vals)))       # 집합 비교: colspan 으로 번진 같은 값 반복은 무시
            if label and sum(len(x) for x in label) >= 3 and nums:
                out.append((label, nums, ln.strip()))
        return out
    ta, tb = table_rows(a), table_rows(b)
    seen = set()
    for la, na, lna in ta:
        cands = [(lb, nb, lnb) for lb, nb, lnb in tb if la <= lb or lb <= la]
        if not cands or any(nb == na for _, nb, _ in cands) or la in seen:
            continue
        # 이름 표기만 다른 같은 행(공단 "단독객실" 6인 = 예약시스템 "단독객실(6인-814호)")은 충돌 아님
        if any(nb == na and (la & lb) for lb, nb, _ in tb):
            continue
        seen.add(la)
        rows.append({"item": " ".join(sorted(la))[:60], "a": lna[:160], "b": cands[0][2][:160],
                     "a_doc": f"{a['src']}:{a['id']}", "b_doc": f"{b['src']}:{b['id']}"})
    return rows[:limit]


def run(apply: bool):
    docs = load()
    print(f"비교 대상 {len(docs)}건 "
          + " · ".join(f"{s} {sum(d['src'] == s for d in docs)}" for s in FILES))
    boiler = boilerplate_lines(docs)
    norm = [normalize(d, boiler) for d in docs]
    sh = [shingles(n) for n in norm]
    hashes = [hashlib.md5(n.encode()).hexdigest() for n in norm]
    pairs = candidates(docs, sh)
    print(f"후보 쌍 {len(pairs)}개 비교 중...")

    # 일반 메뉴 제목: 같은 출처에서 서로 다른 본문 3건 이상이 공유하는 제목(예: 스파이더가 수집한
    # 시설 상세의 제목 "관광시설"). 제목이 정보를 주지 않으므로 이 경우 제목 조건은 본문으로만 판단.
    title_bodies = defaultdict(set)
    for d, h in zip(docs, hashes):
        title_bodies[(d["src"], norm_title(d["title"]))].add(h)
    generic = {k for k, v in title_bodies.items() if len(v) >= 3}

    dup_pairs, partial, series, conflicts, dist = [], [], Counter(), [], Counter()
    for i, j in sorted(pairs):
        a, b = docs[i], docs[j]
        if not norm[i] or not norm[j]:
            continue
        # 예약시스템 상시 페이지끼리는 허용 목록상 서로 다른 메뉴 항목(난바다/허허바다, 탭별 캠핑장 등)
        # — 같은 틀을 공유해 비슷할 뿐 중복이 아니다.
        if a["src"] == b["src"] == "campingkorea" and a["type"] == b["type"] == "facility":
            series["campingkorea(서로 다른 시설·탭)"] += 1
            continue
        cross = a["src"] != b["src"]
        inter = len(sh[i] & sh[j])
        jac = inter / len(sh[i] | sh[j])
        contain = inter / min(len(sh[i]), len(sh[j]))
        tsim = SequenceMatcher(None, norm_title(a["title"]), norm_title(b["title"])).ratio()
        if (a["src"], norm_title(a["title"])) in generic or (b["src"], norm_title(b["title"])) in generic:
            tsim = max(tsim, 1.0 if jac >= JACCARD else tsim)
        exact = hashes[i] == hashes[j]
        dist[round(jac, 1)] += 1
        same_nums = numbers(body_text(a)) == numbers(body_text(b)) or exact
        rec = {"a": i, "b": j, "jaccard": round(jac, 3), "contain": round(contain, 3),
               "title_sim": round(tsim, 2), "exact": exact}
        same_site_boards = not cross and a["type"] == b["type"] == "board"
        if exact or (jac >= JACCARD and tsim >= TITLE_SIM):
            if same_nums:
                dup_pairs.append(rec)
            elif same_site_boards:
                # 같은 사이트 공지인데 숫자만 다름 = 회차·호수가 다른 별개 공고(103호/203호, 1회/2회 합격자)
                series[f"{a['src']}(별개 공고: 회차·호수 다름)"] += 1
            else:
                conflicts.append({**rec, "kind": "거의 같음", "diff": line_conflicts(a, b) or num_diff_rows(a, b)})
        elif contain >= CONTAIN and same_site_boards:
            series[f"{a['src']}(공지 시리즈)"] += 1   # 재공고·취소공고 등 — 요약 관계 아님, 수만 보고
        elif contain >= (CONTAIN_CROSS if cross else CONTAIN):
            partial.append(rec)
            diff = line_conflicts(a, b) if cross else []   # 같은 사이트 부분 중복(연도별 실적표 등)은 충돌 아님
            if diff:
                conflicts.append({**rec, "kind": "부분 중복", "diff": diff})

    # 중복 쌍 → 묶음(union-find) → 묶음마다 남길 문서 1개
    parent = list(range(len(docs)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for p in dup_pairs:
        parent[find(p["a"])] = find(p["b"])
    groups = defaultdict(list)
    for p in dup_pairs:
        groups[find(p["a"])].append(p)
    removals = []
    for root, ps in groups.items():
        members = sorted({x for p in ps for x in (p["a"], p["b"])})
        srcs = {docs[m]["src"] for m in members}
        if len(srcs) == 1:
            keep = min(members, key=lambda m: same_site_rank(docs[m]))
            reason = "같은 사이트 중복 → 원래 메뉴 경로 우선"
        else:
            best = {s: min((m for m in members if docs[m]["src"] == s), key=lambda m: same_site_rank(docs[m]))
                    for s in srcs}
            k, reason = cross_keep(docs[best["dhsisul"]], docs[best["campingkorea"]])
            keep = best[k["src"]]
        for m in members:
            if m == keep:
                continue
            p = next((p for p in ps if {p["a"], p["b"]} == {m, keep}), max(
                (p for p in ps if m in (p["a"], p["b"])), key=lambda p: p["jaccard"]))
            removals.append({"remove": m, "keep": keep, "reason": ("완전 중복" if p["exact"] else "거의 같음")
                             + " / " + reason, "jaccard": p["jaccard"], "title_sim": p["title_sim"]})

    # 제거 예정 사본이 낀 부분 중복·충돌은 남길 원본 쪽 항목과 같은 내용이라 목록에서 뺀다
    removed = {r["remove"] for r in removals}
    partial = [p for p in partial if p["a"] not in removed and p["b"] not in removed]
    conflicts = [c for c in conflicts if c["a"] not in removed and c["b"] not in removed]
    report(docs, removals, partial, conflicts, dist, apply, series)


def _d(docs, i):
    d = docs[i]
    return {"source": d["src"], "doc_id": d["id"], "type": d["type"], "title": d["title"],
            "date": d["date"], "url": d["url"]}


def report(docs, removals, partial, conflicts, dist, apply, series):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "thresholds": {"jaccard": JACCARD, "title_sim": TITLE_SIM, "contain": CONTAIN,
                       "contain_cross": CONTAIN_CROSS, "shingle": SHINGLE},
        "jaccard_distribution_of_candidates": {str(k): v for k, v in sorted(dist.items())},
        "similar_notice_series_pairs": dict(series),
        "removals": [{**r, "remove": _d(docs, r["remove"]), "keep": _d(docs, r["keep"])} for r in removals],
        "partial": [{"a": _d(docs, p["a"]), "b": _d(docs, p["b"]), "jaccard": p["jaccard"],
                     "contain": p["contain"]} for p in partial],
        "conflicts": [{"kind": c["kind"], "a": _d(docs, c["a"]), "b": _d(docs, c["b"]),
                       "jaccard": c["jaccard"], "contain": c["contain"], "diff": c["diff"]} for c in conflicts],
    }
    (OUT_DIR / "dedup_report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [f"# 중복 제거 {'적용' if apply else 'dry-run'} 결과 ({data['generated_at']})", "",
          f"기준: Jaccard ≥ {JACCARD} · 제목 유사도 ≥ {TITLE_SIM} · 부분 포함 ≥ {CONTAIN} (사이트 간 {CONTAIN_CROSS}) · {SHINGLE}-gram", "",
          f"## 제거 예정 {len(removals)}건", "",
          "| 뺄 문서 | 남길 문서 | 판정 이유 | Jaccard | 제목 유사도 |", "|---|---|---|---|---|"]
    for r in data["removals"]:
        md.append(f"| [{r['remove']['source']}] {r['remove']['title'][:40]} (`{r['remove']['doc_id']}`) "
                  f"| [{r['keep']['source']}] {r['keep']['title'][:40]} (`{r['keep']['doc_id']}`) "
                  f"| {r['reason']} | {r['jaccard']} | {r['title_sim']} |")
    md += ["", "※ 같은 사이트 공지끼리 본문이 대부분 겹치는 쌍(재공고·취소공고 시리즈 등, 요약 관계 아님)은 "
           f"목록에서 뺐다: {dict(series)}"]
    md += ["", f"## 부분 중복 {len(partial)}건 (둘 다 남김)", "", "| A | B | 포함률 | Jaccard |", "|---|---|---|---|"]
    for p in data["partial"]:
        md.append(f"| [{p['a']['source']}] {p['a']['title'][:40]} | [{p['b']['source']}] {p['b']['title'][:40]} "
                  f"| {p['contain']} | {p['jaccard']} |")
    md += ["", f"## 충돌 {len(conflicts)}건 (숫자가 다름 — 둘 다 남김, 담당자 확인 필요)", ""]
    for c in data["conflicts"]:
        md.append(f"### [{c['kind']}] {c['a']['title'][:40]} ↔ {c['b']['title'][:40]} "
                  f"(Jaccard {c['jaccard']}, 포함률 {c['contain']})")
        if c["diff"] and "item" in c["diff"][0]:
            md += ["", f"| 항목 | A: [{c['a']['source']}] {c['a']['title'][:24]} | B: [{c['b']['source']}] {c['b']['title'][:24]} |",
                   "|---|---|---|"]
            for row in c["diff"]:
                md.append(f"| {row['item']} | {row['a']} | {row['b']} |")
        else:
            md += ["", "| 문서 | 이쪽에만 있는 숫자 | 문맥 |", "|---|---|---|"]
            for row in c["diff"]:
                md.append(f"| {row['doc']} | {row['only_here']} | {row['context']} |")
        md.append("")
    (OUT_DIR / "dedup_report.md").write_text("\n".join(md), encoding="utf-8")

    print(f"\n제거 예정 {len(removals)}건 · 부분 중복 {len(partial)}건 · 충돌 {len(conflicts)}건")
    print(f"  보고서: {OUT_DIR / 'dedup_report.md'}  (JSON: dedup_report.json)")

    if apply:
        new = [{"source": r["remove"]["source"], "doc_id": r["remove"]["doc_id"],
                "title": r["remove"]["title"], "url": r["remove"]["url"],
                "kept": f"{r['keep']['source']}:{r['keep']['doc_id']}",
                "reason": r["reason"], "jaccard": r["jaccard"], "applied_at": data["generated_at"]}
               for r in data["removals"]]
        # 기존 제외 목록에 '추가'한다 (덮어쓰면 지난번에 적용한 항목이 되살아남)
        prev = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))["excluded"] if EXCLUSIONS.exists() else []
        have = {(e["source"], e["doc_id"]) for e in prev}
        merged = prev + [e for e in new if (e["source"], e["doc_id"]) not in have]
        payload = {"updated_at": data["generated_at"], "thresholds": data["thresholds"], "excluded": merged}
        EXCLUSIONS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log_dir = REPO / "logs"
        log_dir.mkdir(exist_ok=True)
        log = log_dir / f"dedup_applied_{datetime.now():%Y%m%d_%H%M%S}.json"
        log.write_text(json.dumps({"applied_at": data["generated_at"], "thresholds": data["thresholds"],
                                   "excluded": new}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✔ 적용: 이번 {len(new)}건 → {EXCLUSIONS.name} 누적 {len(merged)}건 (이번 분 로그 {log.relative_to(REPO)})")
        print("  → 다음 재적재(ingest.js / ingest_camping.js)부터 반영. 되돌리기: 해당 항목 삭제 후 재적재")


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    run(apply="--apply" in sys.argv)
