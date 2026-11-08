#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR 결과 병합 — 고정 키(doc id) 기준
====================================================================
data/processed/ocr_by_doc.jsonl (줄당 {doc_id, name, text, quality, origin, file?}) 의
quality=='ok' 결과를 data/processed/documents.jsonl 의 해당 문서 본문 끝에
"[이미지: 이름]\\n{OCR텍스트}" 형태로 덧붙인다. (예전 merge 와 같은 형식 — 기존 청크 해시 유지)

매칭은 URL 이 아니라 고정 키(doc id)로 한다. 게시글 URL 에는 목록 위치(startPage·listNo)가 들어 있어
새 글이 올라올 때마다 바뀌므로, URL 매칭은 재수집 후 어긋난다.

main.py 가 전처리 직후 자동 실행(python main.py / python main.py preprocess).
멱등: 이미 [이미지: …] 블록이 붙은 문서는 다시 붙이지 않는다.

  python -m preprocessor.merge_ocr --rebuild <documents.jsonl>
      예전에 병합된 documents.jsonl 의 [이미지: …] 블록에서 ocr_by_doc.jsonl 을 다시 만든다
      (OCR 을 다시 돌리지 않음 — 2026-10 OCR 원본 ocr_results.jsonl 이 없어 이 방법으로 복원)
"""

import json
import re
import sys
from pathlib import Path

import config as C

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

OCR_FILE = C.PROCESSED_DIR / "ocr_by_doc.jsonl"
_MARK = "\n\n[이미지: "


def split_ocr(content: str):
    """병합된 본문 → (OCR 앞 본문, [(이미지이름, 텍스트), ...])"""
    i = content.find(_MARK)
    if i < 0:
        return content, []
    base, tail = content[:i], content[i + 2:]
    blocks = []
    for blk in re.split(r"\n\n(?=\[이미지: )", tail):
        head, _, text = blk.partition("\n")
        m = re.fullmatch(r"\[이미지: (.*)\]", head)
        if m:
            blocks.append((m.group(1), text))
    return base, blocks


def attach(content: str, blocks) -> str:
    add = "\n\n".join(f"[이미지: {name}]\n{text}" for name, text in blocks)
    return (content + "\n\n" + add).strip()


def load_ocr() -> dict:
    """doc_id → [(이름, 텍스트)] (quality=='ok' 만, 파일에 적힌 순서대로)"""
    out = {}
    if OCR_FILE.exists():
        with open(OCR_FILE, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("quality") == "ok" and r.get("text"):
                    out.setdefault(r["doc_id"], []).append((r["name"], r["text"]))
    return out


def ocr_done_ids() -> set:
    """OCR 을 이미 한 문서(품질 낮음·빈 결과 포함) — 다시 돌리지 않는다"""
    if not OCR_FILE.exists():
        return set()
    with open(OCR_FILE, encoding="utf-8") as f:
        return {json.loads(l)["doc_id"] for l in f if l.strip()}


def run_merge():
    docp = C.PROCESSED_DIR / "documents.jsonl"
    if not docp.exists():
        raise FileNotFoundError("documents.jsonl 없음 — preprocess 먼저 실행")
    ocr = load_ocr()
    if not ocr:
        print(f"[merge_ocr] {OCR_FILE.name} 없음/비어 있음 — 병합 건너뜀")
        return 0
    lines, n_doc, n_img, seen = [], 0, 0, set()
    with open(docp, encoding="utf-8") as f:   # JSONL 은 '\n' 로만 구분 (splitlines 는 특수 줄바꿈에서 깨짐)
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            key = d.get("metadata", {}).get("doc_id") or d.get("id")
            blocks = ocr.get(key)
            if blocks and _MARK not in d["content"]:
                d["content"] = attach(d["content"], blocks)
                n_doc += 1
                n_img += len(blocks)
            seen.add(key)
            lines.append(json.dumps(d, ensure_ascii=False))
    docp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    missing = [k for k in ocr if k not in seen]
    print(f"[merge_ocr] 문서 {n_doc}건에 OCR 이미지 {n_img}개 병합 (고정 키 매칭)"
          + (f" · 지금 문서에 없는 키 {len(missing)}건" if missing else ""))
    return n_doc


def rebuild_from(old_jsonl: Path):
    """예전 병합본의 [이미지: …] 블록 → ocr_by_doc.jsonl (고정 키로 변환)"""
    from parser.parser import page_doc_id
    recs = []
    with open(old_jsonl, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            o = json.loads(line)
            m = o.get("metadata", {})
            base, blocks = split_ocr(o["content"])
            if not blocks:
                continue
            old = m.get("doc_id") or o["id"]
            key = page_doc_id(m.get("url", "")) if old.startswith("page-") else old
            for name, text in blocks:
                recs.append({"doc_id": key, "name": name, "text": text, "quality": "ok",
                             "origin": f"rebuilt:{old_jsonl.name}"})
    C.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with open(OCR_FILE, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[merge_ocr] {old_jsonl.name} 에서 문서 {len({r['doc_id'] for r in recs})}건 · 이미지 {len(recs)}개 복원 → {OCR_FILE}")


if __name__ == "__main__":
    if "--rebuild" in sys.argv:
        rebuild_from(Path(sys.argv[sys.argv.index("--rebuild") + 1]))
    else:
        run_merge()
