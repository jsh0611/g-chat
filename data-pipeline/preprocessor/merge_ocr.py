#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR 결과 병합 — ok 품질 OCR 텍스트를 documents.jsonl 에 합친다.
====================================================================
- ocr_results.jsonl 의 quality=='ok' 결과만 대상(노이즈·빈 결과는 제외)
- 각 이미지가 첨부됐던 '원래 게시글 문서'의 content 끝에
  "[이미지: 이름]\n{OCR텍스트}" 형태로 덧붙인다.
- 문서 매칭은 URL 기준(ocr refs.url == documents metadata.url). 검증 결과 223/223 매칭.
- 원본 documents.jsonl 은 documents.jsonl.pre_ocr.bak 로 1회 백업.

실행: python -m preprocessor.merge_ocr
"""

import json
import shutil
import sys

import config as C

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def run_merge():
    docp = C.PROCESSED_DIR / "documents.jsonl"
    ocrp = C.PROCESSED_DIR / "ocr_results.jsonl"
    if not docp.exists():
        raise FileNotFoundError("documents.jsonl 없음 — preprocess 먼저 실행")
    if not ocrp.exists():
        raise FileNotFoundError("ocr_results.jsonl 없음 — OCR 먼저 실행")

    # 1) URL → [(이미지이름, OCR텍스트)] 맵 구성 (ok 품질만)
    # ※ splitlines() 는  ·\x85 같은 특수 줄바꿈에서도 잘려 한 줄 JSON 이 깨진다.
    #    JSONL 은 '\n' 로만 구분되므로 파일을 줄 단위로 순회한다.
    url_map = {}
    with open(ocrp, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("quality") != "ok":
                continue
            text = (r.get("ocr_text") or "").strip()
            if not text:
                continue
            name = r.get("name") or r.get("file")
            for ref in r.get("refs", []):
                u = ref.get("url")
                if u:
                    url_map.setdefault(u, []).append((name, text))

    # 2) documents.jsonl 읽고, 매칭되는 문서에 OCR 텍스트를 덧붙임 (여기도 줄 단위 순회)
    docs = []
    with open(docp, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                docs.append(json.loads(line))

    # 원본 백업(최초 1회만 — 두 번 돌려도 원본은 보존)
    bak = docp.with_name("documents.jsonl.pre_ocr.bak")
    if not bak.exists():
        shutil.copy(docp, bak)

    n_docs_aug = n_snippets = 0
    out_lines = []
    for d in docs:
        url = d.get("metadata", {}).get("url")
        snippets = url_map.get(url)
        if snippets:
            # 한 문서 안에서 같은 (이름,텍스트) 중복 제거
            seen, uniq = set(), []
            for name, text in snippets:
                key = (name, text[:60])
                if key in seen:
                    continue
                seen.add(key)
                uniq.append((name, text))
            add = "\n\n".join(f"[이미지: {name}]\n{text}" for name, text in uniq)
            d["content"] = (d.get("content", "") + "\n\n" + add).strip()
            n_docs_aug += 1
            n_snippets += len(uniq)
        out_lines.append(json.dumps(d, ensure_ascii=False))

    docp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"[merge_ocr] 완료 — 문서 {len(docs)}건 중 {n_docs_aug}건에 "
          f"OCR 이미지텍스트 {n_snippets}조각 병합")
    print(f"           원본 백업: {bak.name}")
    return n_docs_aug


if __name__ == "__main__":
    run_merge()
