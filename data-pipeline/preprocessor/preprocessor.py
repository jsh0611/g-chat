#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
3단계 — 전처리(preprocessor)
====================================================================
역할: data/parsed/documents.json 을 정제해 두 가지를 출력한다.
  1) data/processed/*.txt         — 사람이 읽는 문서별 텍스트(헤더 포함)
  2) data/processed/documents.jsonl — 기계용(라인당 {id, content, metadata})
     → g-chat 의 ingest.js 가 읽어 청킹·임베딩·Supabase(metadata jsonb) 적재
처리: 본문 정제 + 표 평탄화 + 첨부(PDF/HWP) 텍스트·표 추출.
첨부 도구: PDF=pdfplumber, HWP/DOCX 등=LibreOffice(soffice). 없으면 그 첨부만 건너뜀.
"""

import json
import re

import config as C
from preprocessor.extractors import extract as extract_attachment


# ── 텍스트 정제 ─────────────────────────────────────────
_BOILERPLATE = [
    "본문 내용으로 바로가기", "주 메뉴로 바로가기",
    "해당 페이지를 페이스북으로", "오늘 하루 이창을 열지 않음",
]


def clean_text(text: str) -> str:
    lines = []
    raw = [ln.strip() for ln in text.split("\n")]
    skip_next = False
    for i, ln in enumerate(raw):
        if skip_next:
            skip_next = False
            continue
        if not ln or any(b in ln for b in _BOILERPLATE):
            continue
        # 조회수는 다시 받을 때마다 바뀌어, 내용이 그대로인 글도 청크 해시가 달라져 재임베딩된다.
        # 게시글 머리의 "조회수" 줄과 다음 숫자 줄(또는 한 줄 "조회수 123")을 뺀다. (봇 cleanChunk 도 이미 지우던 값)
        if ln == "조회수" and i + 1 < len(raw) and re.fullmatch(r"[\d,]+", raw[i + 1]):
            skip_next = True
            continue
        if re.fullmatch(r"조회수\s*:?\s*[\d,]+", ln):
            continue
        lines.append(ln)
    out = "\n".join(lines)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def flatten_tables(tables: list) -> str:
    blocks = []
    for tbl in tables:
        rows = [" | ".join(c for c in row if c) for row in tbl if any(row)]
        if rows:
            blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


# ── 문서 → 본문/메타데이터 ──────────────────────────────
def _attachment_texts(doc: dict) -> list:
    out = []
    for att in doc.get("attachments", []):
        if att.get("file"):
            atext = extract_attachment(C.DATA / att["file"])
            if atext:
                out.append(f"[첨부: {att.get('name') or att['file']}]\n{atext}")
    return out


def doc_to_content(doc: dict) -> str:
    """임베딩·검색에 쓰일 실제 내용(제목 + 본문 + 표 + 첨부). 메타 라벨은 제외."""
    parts = []
    if doc.get("title"):
        parts.append(doc["title"])
    body = clean_text(doc.get("body", ""))
    if body:
        parts.append(body)
    tbl = flatten_tables(doc.get("tables", []))
    if tbl:
        parts.append("[표]\n" + tbl)
    parts += _attachment_texts(doc)
    return "\n\n".join(parts).strip()


def doc_to_metadata(doc: dict) -> dict:
    meta = {"doc_id": doc.get("id"), "source_type": doc.get("source_type"),
            "title": doc.get("title"), "url": doc.get("url")}
    if doc.get("date"):
        meta["date"] = doc["date"]
    src = doc.get("board") or doc.get("facility")
    if src:
        meta["source"] = src
    return {k: v for k, v in meta.items() if v}


def doc_to_text(doc: dict) -> str:
    """사람이 읽는 .txt (헤더 + 내용)."""
    head = [f"제목: {doc.get('title', '')}"]
    src = doc.get("board") or doc.get("facility") or ""
    head.append(f"출처: {doc.get('source_type', '')}" + (f" / {src}" if src else ""))
    if doc.get("date"):
        head.append(f"날짜: {doc['date']}")
    if doc.get("url"):
        head.append(f"URL: {doc['url']}")
    return "\n".join(head) + "\n" + "=" * 50 + "\n" + doc_to_content(doc) + "\n"


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", s)


def run_preprocess():
    src = C.PARSED_DIR / "documents.json"
    if not src.exists():
        raise FileNotFoundError("documents.json 없음 — 먼저 parser 를 실행하세요.")
    docs = json.loads(src.read_text(encoding="utf-8"))

    C.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_path = C.PROCESSED_DIR / "documents.jsonl"
    n = 0
    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for i, doc in enumerate(docs):
            content = doc_to_content(doc)
            if not content:
                continue
            # 1) 사람이 읽는 txt
            fname = _safe_name(doc.get("id", f"doc_{i}")) + ".txt"
            (C.PROCESSED_DIR / fname).write_text(doc_to_text(doc), encoding="utf-8")
            # 2) 기계용 jsonl (g-chat ingest.js 입력)
            rec = {"id": doc.get("id"), "content": content, "metadata": doc_to_metadata(doc)}
            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1

    print(f"[preprocessor] 완료 — {n}건 → {C.PROCESSED_DIR.name}/*.txt + documents.jsonl")
    return n


if __name__ == "__main__":
    run_preprocess()