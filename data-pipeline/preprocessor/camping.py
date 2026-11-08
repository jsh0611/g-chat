#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
3단계 — 전처리: 동해시 관광지 통합예약시스템(campingkorea.or.kr)
====================================================================
입력: data/parsed/documents_camping.json
출력: data/processed/documents_camping.jsonl  (g-chat 의 ingest_camping.js 입력)
      data/processed/camping/*.txt             (사람이 읽는 문서별 텍스트)

metadata.source 는 항상 'campingkorea' — dhsisul 문서(documents.jsonl)와 구분하는 출처 표시.
  (dhsisul jsonl 은 metadata.source 에 게시판/시설명을 담으므로, 여기서는 그 값을 category 로 둔다)
본문이 비어 있는 게시글(이미지만 올린 공지 등)은 제목만으로는 답할 수 없으므로 내보내지 않고 보고만 한다.
"""

import json
import re

import config as C
from preprocessor.preprocessor import clean_text, _attachment_texts

SRC = C.PARSED_DIR / "documents_camping.json"
OUT = C.PROCESSED_DIR / "documents_camping.jsonl"
TXT_DIR = C.PROCESSED_DIR / "camping"
MIN_BODY_CHARS = 15


def doc_to_content(doc: dict) -> tuple[str, int]:
    parts = [doc["title"]]
    body = clean_text(doc.get("body", ""))
    atts = _attachment_texts(doc)
    if body:
        parts.append(body)
    parts += atts
    # 이미지만 올린 글은 본문 자리에 제목만 한 번 더 있다("추암오토캠핑장 사이트 규격안내도") → 본문 없음으로 취급
    bare = re.sub(r"\s+", "", body)
    title_core = re.sub(r"\s+", "", re.sub(r"^\[[^\]]*\]", "", doc["title"]))
    substance = (0 if bare and bare in title_core else len(body)) + sum(len(a) for a in atts)
    return "\n\n".join(parts).strip(), substance


def doc_to_metadata(doc: dict) -> dict:
    meta = {"doc_id": doc["id"], "source_type": doc["source_type"], "source": C.CK_SOURCE,
            "kind": doc.get("kind"), "category": doc.get("category"), "title": doc["title"],
            "url": doc["url"], "date": doc.get("date") or None,
            "reserve_url": doc.get("reserve_url"), "external_url": doc.get("external_url"),
            "pinned": doc.get("pinned") or None,     # 상단 고정 공지 = 옛 날짜여도 현재 유효
            "attachments": [a.get("name") or a.get("file") for a in doc.get("attachments", [])
                            if a.get("file")] or None}
    return {k: v for k, v in meta.items() if v}


def run_preprocess_camping():
    if not SRC.exists():
        raise FileNotFoundError("documents_camping.json 없음 — 먼저 'python main.py camping-parse' 실행")
    docs = json.loads(SRC.read_text(encoding="utf-8"))
    TXT_DIR.mkdir(parents=True, exist_ok=True)
    n, empty = 0, []
    with open(OUT, "w", encoding="utf-8") as jf:
        for doc in docs:
            content, substance = doc_to_content(doc)
            if substance < MIN_BODY_CHARS:
                empty.append(f"{doc['title']} ({doc['url']})"
                             + (" — 본문 이미지만 있음" if doc.get("images") else ""))
                continue
            meta = doc_to_metadata(doc)
            head = [f"제목: {doc['title']}", f"출처: {C.CK_SOURCE} / {doc.get('category', '')}"]
            if doc.get("date"):
                head.append(f"날짜: {doc['date']}")
            head.append(f"URL: {doc['url']}")
            (TXT_DIR / (re.sub(r'[\\/:*?"<>|]', "_", doc["id"]) + ".txt")).write_text(
                "\n".join(head) + "\n" + "=" * 50 + "\n" + content + "\n", encoding="utf-8")
            jf.write(json.dumps({"id": doc["id"], "content": content, "metadata": meta},
                                ensure_ascii=False) + "\n")
            n += 1
    print(f"[preprocessor:예약시스템] 완료 — {n}건 → {OUT.name}")
    if empty:
        print(f"   ↳ 본문 없음으로 제외 {len(empty)}건:")
        for e in empty:
            print("     -", e)
    return n


if __name__ == "__main__":
    run_preprocess_camping()
