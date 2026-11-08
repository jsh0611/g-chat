#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
파이프라인 실행기
====================================================================
  python main.py                # 전체: 수집 → 구조화 → 전처리
  python main.py crawl          # 수집만 (증분: 새 글만 추가)
  python main.py parse          # 구조화만 (수집 결과 필요)
  python main.py preprocess     # 전처리만 (구조화 결과 필요)
  python main.py crawl --no-download   # HTML 만 받고 첨부 다운로드 생략
  python main.py crawl --reset         # 기존 manifest 무시하고 처음부터
  python main.py ocr-new               # OCR 결과 없는 문서의 이미지만 OCR → 병합 (pytesseract 필요)
  ※ preprocess 뒤에는 항상 OCR 병합(preprocessor/merge_ocr.py, 고정 키 매칭)이 자동 실행된다

  [두 번째 출처: 동해시 관광지 통합예약시스템 campingkorea.or.kr — 허용 목록 수집]
  python main.py camping               # 수집 → 구조화 → 전처리 (documents_camping.jsonl)
  python main.py camping-crawl         # 수집만 (증분, --reset / --no-download 동일하게 사용)
  python main.py camping-parse         # 구조화만
  python main.py camping-preprocess    # 전처리만
"""

import sys

# Windows 콘솔/파일 리다이렉트 시 기본 인코딩(cp949)으로는 '↳ ✔ ⚠' 등을 못 찍어
# UnicodeEncodeError 로 파이프라인 전체가 죽는다(수집은 끝났는데 parse/preprocess 미실행).
# 출력 스트림을 UTF-8 로 고정해 방지.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from crawler.crawler import run_crawl
from parser.parser import run_parse
from preprocessor.preprocessor import run_preprocess
from preprocessor.merge_ocr import run_merge
from crawler.camping import run_crawl_camping
from parser.camping import run_parse_camping
from preprocessor.camping import run_preprocess_camping


def main(argv):
    stage = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else "all"
    download = "--no-download" not in argv
    full_reset = "--reset" in argv

    if stage in ("all", "crawl"):
        run_crawl(download=download, full_reset=full_reset)
    if stage in ("all", "parse"):
        run_parse()
    if stage in ("all", "preprocess"):
        run_preprocess()
        run_merge()        # OCR 결과(ocr_by_doc.jsonl)를 고정 키로 병합 — 빠지면 OCR 문서 청크가 사라짐
    if stage == "ocr-new":
        # OCR 결과가 없는 문서의 이미지·스캔 PDF 만 Tesseract 로 처리 후 다시 병합
        # (pytesseract 가 있는 파이썬으로 실행 — 이 PC 는 agent_env)
        from preprocessor.ocr import run_ocr_missing
        run_ocr_missing()
        run_merge()

    if stage in ("camping", "camping-crawl"):
        run_crawl_camping(download=download, full_reset=full_reset)
    if stage in ("camping", "camping-parse"):
        run_parse_camping()
    if stage in ("camping", "camping-preprocess"):
        run_preprocess_camping()

    if stage not in ("all", "crawl", "parse", "preprocess", "ocr-new",
                     "camping", "camping-crawl", "camping-parse", "camping-preprocess"):
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)